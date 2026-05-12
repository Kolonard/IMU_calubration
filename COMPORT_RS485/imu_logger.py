"""
imu_logger.py
=============
Асинхронный логгер потока IMU через RS-485/USB @ 921600 бод.
Интегрирован с системой синхронизации (sync_marks.py / SYNC_STORE).

Протокол пакета (32 байта):
  Header  2B  \xC0\xC0
  Gx      4B  int32
  Gy      4B  int32
  Gz      4B  int32
  Ax      4B  int32
  Ay      4B  int32
  Az      4B  int32
  TC      2B  int16
  Counter 1B  uint8
  Status  1B  uint8
  CRC     2B  uint16  CRC16-CCITT над байтами [2:-2]

Формат записи в .bin (54 байта на пакет):
  Magic       4B  "DREC"
  recv_ts_ns  8B  uint64  время приёма (perf_counter_ns + wall_offset)
  sync_seq    4B  uint32  номер ближайшего синхроимпульса (0xFFFFFFFF = нет)
  sync_ts_ns  8B  int64   NTP-время синхроимпульса
  delta_us    2B  int16   отклонение recv от sync, мкс
  raw        32B  байты пакета (с заголовком и CRC)
  ─────────────────────────────────────────────────
  Итого:     58B

Заголовок файла (32 байта):
  magic       8B  "IMULOG01"
  version     2B  uint16 = 1
  frame_sz    2B  uint16 = 32
  record_sz   2B  uint16 = 58
  sample_hz   4B  uint32 = 2000
  wall_epoch  8B  int64   time.time_ns() при старте
  perf_start  8B  uint64  perf_counter_ns() при старте
  reserved    6B  = 0
  ─────────────────────────────────────────────────
  Итого:     40B (дополнен до 64 нулями)
"""

import asyncio
import struct
import time
import threading
import collections
import logging
import sys
from pathlib import Path
from typing import Optional

import serial_asyncio

# ── Импорт хранилища синхро-меток ────────────────────────────────
# Если запускаешь логгер вместе с sync_generator в одном процессе —
# SYNC_STORE будет заполняться автоматически.
# Если в разных процессах — замени на UDP-приёмник (см. ниже).
try:
    from sync_marks import SYNC_STORE, SyncMark
    SYNC_AVAILABLE = True
except ImportError:
    SYNC_AVAILABLE = False
    SYNC_STORE = None

log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ══════════════════════════════════════════════════════════════════
PORT            = "COM3"
BAUDRATE        = 921600

SYNC_BYTES      = b'\xC0\xC0'
FRAME_SIZE      = 32
PACKET_STRUCT   = struct.Struct('<H6ih2BH')   # 32 байта

OUTPUT_FILE     = "data_async.bin"

# Буфер записи на диск: сбрасываем при накоплении N записей или каждые T сек
FLUSH_RECORDS   = 400          # ~0.2 сек при 2кГц
FLUSH_INTERVAL  = 0.5          # сек

# Максимальное отклонение от sync-метки, при котором пакет считается синхронизированным
MAX_SYNC_GAP_NS = 5_000_000    # 5 мс (покрывает GIL-jitter на Windows) (3 × период 2кГц)

STAT_INTERVAL   = 5.0          # сек между выводом статистики
# ══════════════════════════════════════════════════════════════════

# ─── Форматы бинарных структур ───────────────────────────────────
FILE_HEADER_FMT    = struct.Struct('<8sHHHIqQ6x')   # 40 байт → pad до 64
FILE_HEADER_SIZE   = 64

RECORD_HEADER      = b"DREC"
RECORD_FMT         = struct.Struct('<4sQIqh32s')     # 4+8+4+8+2+32 = 58 байт
RECORD_SIZE        = RECORD_FMT.size                 # 58

UNSYNC_SEQ         = 0xFFFF_FFFF
UNSYNC_DELTA       = -32768


def crc16_ccitt(data: bytes, poly: int = 0x1021, init: int = 0xFFFF) -> int:
    crc = init
    for b in data:
        crc ^= (b << 8)
        for _ in range(8):
            crc = ((crc << 1) ^ poly if crc & 0x8000 else crc << 1) & 0xFFFF
    return crc


# ══════════════════════════════════════════════════════════════════
#  Привязка perf_counter к wall-clock
#  perf_counter_ns используется для точных меток внутри файла,
#  wall_epoch сохраняется в заголовке для привязки к реальному времени.
# ══════════════════════════════════════════════════════════════════

class WallClock:
    """
    Однократно калибруется при старте.
    Даёт абсолютное время в нс через perf_counter_ns (монотонный).
    """
    def __init__(self):
        # Берём несколько измерений и выбираем с минимальным jitter
        samples = []
        for _ in range(20):
            t0 = time.perf_counter_ns()
            wall = time.time_ns()
            t1 = time.perf_counter_ns()
            if t1 - t0 < 5000:   # принимаем только пары с разницей < 5 мкс
                samples.append((wall, (t0 + t1) // 2))

        if not samples:
            wall, perf = time.time_ns(), time.perf_counter_ns()
        else:
            wall, perf = samples[0]

        self.wall_epoch_ns: int = wall    # time.time_ns() при старте
        self.perf_start_ns: int = perf    # perf_counter_ns() при старте
        self._offset_ns: int = wall - perf

    def now_ns(self) -> int:
        """Монотонное время с абсолютной привязкой (нс)."""
        return time.perf_counter_ns() + self._offset_ns


WALL_CLOCK = WallClock()


# ══════════════════════════════════════════════════════════════════
#  Sync mark lookup
# ══════════════════════════════════════════════════════════════════

def _find_sync(recv_ts_ns: int):
    """
    Вернуть (sync_seq, sync_ts_ns, delta_us) для заданного времени приёма.
    Если SYNC_STORE недоступен или метка слишком далеко — вернуть sentinel.
    """
    if not SYNC_AVAILABLE or SYNC_STORE is None:
        return UNSYNC_SEQ, recv_ts_ns, UNSYNC_DELTA

    mark = SYNC_STORE.get_nearest(recv_ts_ns)
    if mark is None:
        return UNSYNC_SEQ, recv_ts_ns, UNSYNC_DELTA

    delta_ns = recv_ts_ns - mark.ts_ns
    if abs(delta_ns) > MAX_SYNC_GAP_NS:
        return mark.seq, mark.ts_ns, UNSYNC_DELTA   # метка есть, но далеко

    delta_us = max(-32767, min(32767, int(delta_ns / 1000)))
    return mark.seq, mark.ts_ns, delta_us


# ══════════════════════════════════════════════════════════════════
#  Статистика (lock-free через deque)
# ══════════════════════════════════════════════════════════════════

class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self.total = 0
        self.crc_errors = 0
        self.loss_count = 0
        self.unsync_count = 0
        self._ts_window: collections.deque = collections.deque(maxlen=4000)

    def record(self, recv_ts_ns: int, crc_ok: bool,
               loss: int, synced: bool):
        with self._lock:
            self.total += 1
            self._ts_window.append(recv_ts_ns)
            if not crc_ok:
                self.crc_errors += 1
            self.loss_count += loss
            if not synced:
                self.unsync_count += 1

    def snapshot(self) -> dict:
        with self._lock:
            n = len(self._ts_window)
            if n > 1:
                dt_s = (self._ts_window[-1] - self._ts_window[0]) / 1e9
                rate = (n - 1) / dt_s if dt_s > 0 else 0.0
            else:
                rate = 0.0
            return {
                "total":   self.total,
                "rate":    rate,
                "crc_err": self.crc_errors,
                "loss":    self.loss_count,
                "unsync":  self.unsync_count,
            }


# ══════════════════════════════════════════════════════════════════
#  Disk writer (отдельный поток, буферизованная запись)
# ══════════════════════════════════════════════════════════════════

class DiskWriter:
    """
    Принимает готовые байтовые записи через put(),
    пишет на диск батчами для минимизации системных вызовов.
    """
    def __init__(self, path: str):
        self._path = path
        self._queue: collections.deque = collections.deque()
        self._event = threading.Event()
        self._stop  = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="DiskWriter"
        )

    def start(self, file_header: bytes):
        self._file_header = file_header
        self._thread.start()

    def put(self, record: bytes):
        self._queue.append(record)
        if len(self._queue) >= FLUSH_RECORDS:
            self._event.set()

    def stop(self):
        self._stop.set()
        self._event.set()
        self._thread.join(timeout=5.0)

    def _loop(self):
        with open(self._path, "wb") as f:
            # ── Заголовок файла ──────────────────────────────────
            header_data = FILE_HEADER_FMT.pack(
                b"IMULOG01",
                1,                          # version
                FRAME_SIZE,
                RECORD_SIZE,
                2000,                       # sample_hz
                WALL_CLOCK.wall_epoch_ns,
                WALL_CLOCK.perf_start_ns,
            )
            # Дополняем до FILE_HEADER_SIZE нулями
            header_padded = header_data + b'\x00' * (FILE_HEADER_SIZE - len(header_data))
            f.write(header_padded)
            f.flush()

            last_flush = time.monotonic()

            while not self._stop.is_set():
                self._event.wait(timeout=FLUSH_INTERVAL)
                self._event.clear()

                # Забираем всё из очереди одним батчем
                batch = []
                while self._queue:
                    try:
                        batch.append(self._queue.popleft())
                    except IndexError:
                        break

                if batch:
                    f.write(b"".join(batch))
                    f.flush()

            # Финальный дренаж
            while self._queue:
                try:
                    f.write(self._queue.popleft())
                except IndexError:
                    break
            f.flush()

        log.info("DiskWriter: файл закрыт.")


# ══════════════════════════════════════════════════════════════════
#  asyncio Protocol
# ══════════════════════════════════════════════════════════════════

class IMUProtocol(asyncio.Protocol):
    """
    Асинхронный парсер потока IMU.
    Поиск SYNC-байт → валидация CRC → прикрепление sync-метки → запись.
    """

    def __init__(self, writer: DiskWriter, stats: Stats):
        self._buf    = bytearray()
        self._writer = writer
        self._stats  = stats

        self._prev_counter: Optional[int] = None
        self._transport = None

    def connection_made(self, transport):
        self._transport = transport
        log.info("COM-порт открыт: %s @ %d", PORT, BAUDRATE)

    def connection_lost(self, exc):
        log.warning("COM-порт закрыт: %s", exc)

    def data_received(self, data: bytes):
        self._buf += data
        self._parse()

    def _parse(self):
        buf = self._buf

        while True:
            # ── 1. Найти заголовок ───────────────────────────────
            idx = buf.find(SYNC_BYTES)
            if idx == -1:
                # Нет заголовка — сохраняем хвост на случай разрыва
                self._buf = buf[-1:] if buf else bytearray()
                return

            if idx > 0:
                # Мусор перед заголовком
                log.debug("Пропущено %d байт мусора", idx)
                buf = buf[idx:]

            # ── 2. Достаточно данных? ────────────────────────────
            if len(buf) < FRAME_SIZE:
                self._buf = buf
                return

            # ── 3. Извлечь кандидата ─────────────────────────────
            frame = bytes(buf[:FRAME_SIZE])

            # ── 4. Валидация CRC ─────────────────────────────────
            crc_received = struct.unpack_from('<H', frame, FRAME_SIZE - 2)[0]
            crc_computed  = crc16_ccitt(frame[2:FRAME_SIZE - 2])
            crc_ok = (crc_received == crc_computed)

            if not crc_ok:
                # Сдвигаемся на 1 байт — ищем следующий заголовок
                # (защита от ложного SYNC внутри данных)
                log.debug("CRC mismatch — сдвиг на 1 байт")
                buf = buf[1:]
                continue

            # ── 5. CRC верен — принимаем пакет ───────────────────
            recv_ts_ns = WALL_CLOCK.now_ns()
            buf = buf[FRAME_SIZE:]    # сдвигаем буфер

            # ── 6. Распаковка для счётчика потерь ────────────────
            fields = PACKET_STRUCT.unpack(frame)
            # fields: header, Gx, Gy, Gz, Ax, Ay, Az, TC, Counter, Status, CRC
            counter = fields[8]   # uint8

            loss = 0
            if self._prev_counter is not None:
                expected = (self._prev_counter + 1) & 0xFF
                if counter != expected:
                    loss = (counter - expected) & 0xFF
            self._prev_counter = counter

            # ── 7. Прикрепить sync-метку ─────────────────────────
            sync_seq, sync_ts_ns, delta_us = _find_sync(recv_ts_ns)
            synced = (delta_us != UNSYNC_DELTA)

            # ── 8. Собрать запись и отправить на диск ────────────
            record = RECORD_FMT.pack(
                RECORD_HEADER,
                recv_ts_ns,
                sync_seq,
                sync_ts_ns,
                delta_us,
                frame,
            )
            self._writer.put(record)

            # ── 9. Статистика ────────────────────────────────────
            self._stats.record(recv_ts_ns, crc_ok, loss, synced)

        # Сохраняем хвост буфера
        self._buf = buf


# ══════════════════════════════════════════════════════════════════
#  Периодический вывод статистики
# ══════════════════════════════════════════════════════════════════

async def stat_printer(stats: Stats):
    while True:
        await asyncio.sleep(STAT_INTERVAL)
        s = stats.snapshot()
        sync_pct = (1 - s["unsync"] / s["total"]) * 100 if s["total"] else 0
        log.info(
            "Rate: %.0f pkt/s | Total: %d | "
            "Loss: %d | CRC err: %d | Sync: %.1f%%",
            s["rate"], s["total"],
            s["loss"], s["crc_err"], sync_pct,
        )


# ══════════════════════════════════════════════════════════════════
#  Утилита: чтение .bin для проверки
# ══════════════════════════════════════════════════════════════════

def read_bin(path: str, max_records: int = 20):
    """
    Читает и выводит первые N записей из .bin файла.
    Использование: python imu_logger.py --read data_async.bin
    """
    Gmult = 1.085069e-6   # рад/с
    Amult = 5e-5          # м/с²
    Tmult = 0.01          # °C

    with open(path, "rb") as f:
        # Заголовок файла
        hdr_raw = f.read(FILE_HEADER_SIZE)
        hdr = FILE_HEADER_FMT.unpack(hdr_raw[:FILE_HEADER_FMT.size])
        magic, version, frame_sz, record_sz, sample_hz, wall_epoch, perf_start = hdr
        print(f"Файл: {path}")
        print(f"  Magic:      {magic}")
        print(f"  Version:    {version}")
        print(f"  Frame sz:   {frame_sz} B")
        print(f"  Record sz:  {record_sz} B")
        print(f"  Sample Hz:  {sample_hz}")
        import datetime
        dt = datetime.datetime.fromtimestamp(wall_epoch / 1e9)
        print(f"  Записан:    {dt:%Y-%m-%d %H:%M:%S.%f}")
        print()

        print(f"{'№':>5} {'recv_ms':>10} {'sync_seq':>8} "
              f"{'Δ,µs':>7} {'Gx':>8} {'Gy':>8} {'Gz':>8} "
              f"{'Ax':>8} {'Ay':>8} {'Az':>8} "
              f"{'T,°C':>7} {'Cnt':>4} {'St':>3} {'CRC':>3}")
        print("─" * 120)

        t0 = None
        count = 0

        while count < max_records:
            rec_raw = f.read(RECORD_SIZE)
            if len(rec_raw) < RECORD_SIZE:
                break

            rec = RECORD_FMT.unpack(rec_raw)
            rec_magic, recv_ts_ns, sync_seq, sync_ts_ns, delta_us, raw = rec

            if rec_magic != RECORD_HEADER:
                print(f"[!] Неверный маркер записи: {rec_magic}")
                break

            if t0 is None:
                t0 = recv_ts_ns
            recv_ms = (recv_ts_ns - t0) / 1e6

            fields = PACKET_STRUCT.unpack(raw)
            _, Gx, Gy, Gz, Ax, Ay, Az, TC, Counter, Status, CRC = fields

            crc_ok = crc16_ccitt(raw[2:-2]) == CRC
            sync_str = f"{delta_us:+d}" if delta_us != UNSYNC_DELTA else "—"

            print(f"{count:5d} {recv_ms:10.3f} {sync_seq:8d} "
                  f"{sync_str:>7} "
                  f"{Gx:8d} {Gy:8d} {Gz:8d} "
                  f"{Ax:8d} {Ay:8d} {Az:8d} "
                  f"{TC*Tmult:7.2f} {Counter:4d} {Status:3d} "
                  f"{'OK' if crc_ok else 'ERR':>3}")
            count += 1

    print(f"\nВыведено {count} записей из {path}")


# ══════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════

async def run_logger():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d  %(message)s",
        datefmt="%H:%M:%S",
    )

    stats  = Stats()
    writer = DiskWriter(OUTPUT_FILE)
    writer.start(b"")   # заголовок пишется внутри DiskWriter._loop

    loop = asyncio.get_running_loop()

    transport, protocol = await serial_asyncio.create_serial_connection(
        loop,
        lambda: IMUProtocol(writer, stats),
        PORT,
        baudrate=BAUDRATE,
    )

    log.info("Логгер запущен → %s", OUTPUT_FILE)
    log.info("SYNC_STORE: %s", "подключён" if SYNC_AVAILABLE else "НЕ подключён")

    try:
        stat_task = asyncio.create_task(stat_printer(stats))
        await asyncio.Future()   # run forever
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        stat_task.cancel()
        transport.close()
        writer.stop()
        s = stats.snapshot()
        log.info(
            "Завершение. Итого пакетов: %d | Потери: %d | CRC ошибок: %d",
            s["total"], s["loss"], s["crc_err"],
        )


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3 and sys.argv[1] == "--read":
        n = int(sys.argv[3]) if len(sys.argv) > 3 else 20
        read_bin(sys.argv[2], max_records=n)
    else:
        asyncio.run(run_logger())
