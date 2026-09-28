"""
serial_reader.py
Читает поток данных со стенда через RS-485→USB @ 921600 бод.
Пакет: 32 байта, 2кГц → ожидаемый интервал ~0.5мс.
К каждому пакету прикрепляется ближайшая синхро-метка из SYNC_STORE.
Готов к интеграции с твоим бинарным логгером.
"""

import time
import serial
import struct
import threading
import logging
from dataclasses import dataclass
from typing import Callable
from sync_marks import SYNC_STORE, SyncMark

log = logging.getLogger(__name__)

# ═══════════════════════════ НАСТРОЙКИ ═══════════════════════════
RS485_PORT      = "/dev/ttyUSB0"    # или "COM4" на Windows
RS485_BAUDRATE  = 921600
PACKET_SIZE     = 32

# Максимальное время (нс), в которое ищем ближайшую sync-метку.
# Если ближайшая метка дальше этого — помечаем как UNSYNC.
MAX_SYNC_GAP_NS = 1_000_000        # 1 мс
# ═════════════════════════════════════════════════════════════════


@dataclass
class DataPacket:
    """Один принятый пакет данных со стенда."""
    seq: int                # порядковый номер пакета данных
    recv_ts_ns: int         # время приёма (time.time_ns())
    raw: bytes              # сырые 32 байта
    sync_mark: SyncMark | None   # ближайший синхроимпульс
    sync_delta_us: float | None  # отклонение от метки, мкс


def _attach_sync(recv_ts_ns: int) -> tuple[SyncMark | None, float | None]:
    """Найти ближайшую sync-метку и вычислить отклонение."""
    mark = SYNC_STORE.get_nearest(recv_ts_ns)
    if mark is None:
        return None, None
    delta_ns = recv_ts_ns - mark.ts_ns
    if abs(delta_ns) > MAX_SYNC_GAP_NS:
        return mark, None   # метка есть, но слишком далеко
    return mark, delta_ns / 1e3


class RS485Reader:
    """
    Читает RS-485 поток, разбивает на пакеты по PACKET_SIZE байт,
    вызывает on_packet(DataPacket) для каждого принятого пакета.
    """
    def __init__(self, on_packet: Callable[[DataPacket], None]):
        self.on_packet = on_packet
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run(self):
        port = serial.Serial(
            RS485_PORT,
            baudrate=RS485_BAUDRATE,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.01          # 10 мс read timeout
        )
        log.info("RS-485 открыт: %s @ %d бод", RS485_PORT, RS485_BAUDRATE)

        buf = bytearray()
        seq = 0

        # ── Опционально: найти начало потока по заголовку пакета ──
        # Если у твоих пакетов есть фиксированный заголовок — раскомментируй:
        # PACKET_HEADER = b"\xAA\x55"
        # _sync_to_header(port, buf, PACKET_HEADER)

        try:
            while not self._stop.is_set():
                chunk = port.read(PACKET_SIZE - len(buf))
                if not chunk:
                    continue
                buf.extend(chunk)

                if len(buf) >= PACKET_SIZE:
                    raw = bytes(buf[:PACKET_SIZE])
                    recv_ts_ns = time.time_ns()
                    buf = buf[PACKET_SIZE:]

                    mark, delta_us = _attach_sync(recv_ts_ns)

                    pkt = DataPacket(
                        seq=seq,
                        recv_ts_ns=recv_ts_ns,
                        raw=raw,
                        sync_mark=mark,
                        sync_delta_us=delta_us,
                    )
                    seq += 1

                    try:
                        self.on_packet(pkt)
                    except Exception as e:
                        log.error("on_packet error: %s", e)

        except serial.SerialException as e:
            log.error("RS-485: %s", e)
        finally:
            port.close()
            log.info("RS-485 закрыт. Принято пакетов: %d", seq)


def _sync_to_header(port: serial.Serial, buf: bytearray, header: bytes):
    """Сдвинуть буфер до начала пакета по сигнатуре заголовка."""
    log.info("Синхронизация с потоком по заголовку %s ...", header.hex())
    window = bytearray()
    while True:
        b = port.read(1)
        if not b:
            continue
        window.extend(b)
        if len(window) > len(header) * 4:
            window = window[-len(header):]
        if bytes(window[-len(header):]) == header:
            log.info("Заголовок найден.")
            break