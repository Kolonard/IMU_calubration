"""
run_loopback_test.py  —  Windows 11
=====================================
Полный end-to-end тест БЕЗ XP VM.
Всё крутится на одном ПК через loopback (127.0.0.1).

Что происходит:
  1. Запускает встроенный эмулятор стенда (TCP-сервер :55200)
  2. Запускает inline sync-генератор (пишет в SYNC_STORE, нет UDP)
  3. Подключает tcp_adapter (читает TCP, пишет .bin)
  4. Через DURATION секунд — останавливает всё
  5. Читает data_loopback.bin и валидирует
  6. Печатает итоговый отчёт

Запуск:
    python run_loopback_test.py
    python run_loopback_test.py --duration 30 --rate 2000 --frames 5000
    python run_loopback_test.py --file data.bin     # реальные данные из .bin
"""

import asyncio
import socket
import struct
import threading
import time
import sys
import os
import math
import argparse
import logging

# ── Импорты из проекта ────────────────────────────────────────────────────────
from sync_marks  import SYNC_STORE, SyncMark
from imu_logger  import (IMUProtocol, DiskWriter, Stats,
                          stat_printer, crc16_ccitt,
                          RECORD_FMT, RECORD_SIZE, FILE_HEADER_SIZE,
                          UNSYNC_DELTA)

log = logging.getLogger("loopback")

# ─────────────────────────────────────────────────────────────────────────────
# Настройки теста
# ─────────────────────────────────────────────────────────────────────────────
TCP_PORT    = 55299          # loopback-порт (не трогает 55200)
OUTPUT_FILE = "data_loopback.bin"
FRAME_SIZE  = 32

PACKET_STRUCT = struct.Struct('<H6ih2BH')   # весь 32-байтный пакет


# ─────────────────────────────────────────────────────────────────────────────
# Генератор синтетических кадров (со стороны стенда)
# ─────────────────────────────────────────────────────────────────────────────

def _crc(data):
    return crc16_ccitt(data)


def make_fake_frame(counter, t):
    """Синтетический IMU-пакет с валидным CRC."""
    gx = int(math.sin(t * 2.0)  * 10000)
    gy = int(math.cos(t * 3.0)  * 8000)
    gz = int(math.sin(t * 1.5)  * 6000)
    ax = int(math.cos(t * 0.5)  * 50000)
    ay = int(math.sin(t * 0.7)  * 40000)
    az = 98000 + int(math.cos(t * 1.1) * 2000)
    tc = 2500
    cnt    = counter & 0xFF
    status = 0

    payload = struct.pack('<H6ih2B',
                          0xC0C0, gx, gy, gz, ax, ay, az, tc, cnt, status)
    crc = _crc(payload[2:])
    return payload + struct.pack('<H', crc)


def load_raw_frames(path):
    """
    Загружает 32-байтные кадры из .bin файла.
    Автоматически определяет наш формат (IMULOG01) или raw.
    """
    frames = []
    with open(path, 'rb') as f:
        magic = f.read(8)
        if magic == b'IMULOG01':
            f.seek(FILE_HEADER_SIZE)
            while True:
                rec = f.read(RECORD_SIZE)
                if len(rec) < RECORD_SIZE:
                    break
                if rec[:4] == b'DREC':
                    frames.append(rec[RECORD_SIZE - FRAME_SIZE:])
        else:
            f.seek(0)
            while True:
                fr = f.read(FRAME_SIZE)
                if len(fr) < FRAME_SIZE:
                    break
                frames.append(fr)
    return frames


# ─────────────────────────────────────────────────────────────────────────────
# Эмулятор стенда (TCP-сервер)
# ─────────────────────────────────────────────────────────────────────────────

def bench_server_thread(frames, rate_hz, ready_evt, stop_evt):
    """
    TCP-сервер: принимает одно подключение, шлёт кадры с заданной частотой.
    При исчерпании frames — зацикливает.
    """
    period = 1.0 / rate_hz
    n      = len(frames)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', TCP_PORT))
    srv.listen(1)
    ready_evt.set()

    srv.settimeout(10.0)
    try:
        conn, addr = srv.accept()
    except socket.timeout:
        log.error("Bench: никто не подключился за 10 секунд")
        srv.close()
        return

    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
    log.info("Bench: клиент подключился %s", addr)

    idx       = 0
    next_tick = time.perf_counter()

    try:
        while not stop_evt.is_set():
            next_tick += period
            # Грубый sleep + busy-wait
            rem = next_tick - time.perf_counter()
            if rem > 0.001:
                time.sleep(rem - 0.001)
            while time.perf_counter() < next_tick:
                pass

            conn.sendall(frames[idx % n])
            idx += 1
    except (socket.error, OSError):
        pass
    finally:
        conn.close()
        srv.close()
        log.info("Bench: отправлено %d кадров", idx)


# ─────────────────────────────────────────────────────────────────────────────
# Inline sync-генератор (пишет в SYNC_STORE, без UDP)
# ─────────────────────────────────────────────────────────────────────────────

def sync_gen_thread(rate_hz, stop_evt):
    """
    Генератор меток синхронизации.
    Busy-wait loop → пишет SyncMark прямо в SYNC_STORE.
    (UDP не нужен — всё в одном процессе.)
    """
    period_ns = 1_000_000_000 // rate_hz
    seq       = 0
    now       = time.perf_counter_ns()
    next_tick = ((now // period_ns) + 1) * period_ns

    while not stop_evt.is_set():
        while time.perf_counter_ns() < next_tick:
            pass
        fire_ns = time.perf_counter_ns()
        SYNC_STORE.put(SyncMark(seq=seq, ts_ns=fire_ns, perf_ns=fire_ns))
        seq += 1
        next_tick += period_ns

    log.info("Sync-gen: остановлен, записано %d меток", seq)


# ─────────────────────────────────────────────────────────────────────────────
# TCP-receiver (asyncio, читает из bench_server_thread)
# ─────────────────────────────────────────────────────────────────────────────

async def tcp_receiver(writer, stats, duration_s):
    protocol = IMUProtocol(writer, stats)

    log.info("TCP: подключение к 127.0.0.1:%d ...", TCP_PORT)
    reader, w = await asyncio.open_connection('127.0.0.1', TCP_PORT)
    log.info("TCP: подключено. Тест %.0f сек.", duration_s)

    deadline = asyncio.get_event_loop().time() + duration_s
    try:
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                data = await asyncio.wait_for(reader.read(8192), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if not data:
                log.warning("TCP: соединение закрыто")
                break
            protocol.data_received(data)
    finally:
        try:
            w.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Валидация выходного .bin файла
# ─────────────────────────────────────────────────────────────────────────────

def validate_output(path):
    """
    Читает data_loopback.bin и проверяет:
      - корректность заголовка файла
      - CRC каждого пакета
      - наличие sync-меток
      - монотонность временны́х меток
      - потери пакетов (поле Counter)
    Возвращает dict с результатами.
    """
    result = {
        "records":    0,
        "crc_ok":     0,
        "crc_err":    0,
        "synced":     0,
        "unsynced":   0,
        "pkt_loss":   0,
        "ts_jumps":   0,   # временны́е метки пошли назад
        "header_ok":  False,
    }

    if not os.path.exists(path):
        result["error"] = "Файл не найден: " + path
        return result

    with open(path, 'rb') as f:
        # ── Заголовок файла ──────────────────────────────────
        hdr = f.read(FILE_HEADER_SIZE)
        if len(hdr) < 8:
            result["error"] = "Файл слишком короткий"
            return result

        magic = hdr[:8]
        if magic == b'IMULOG01':
            result["header_ok"] = True
        else:
            result["error"] = "Неверный magic: " + repr(magic)
            return result

        # ── Записи ───────────────────────────────────────────
        prev_ts  = None
        prev_cnt = None

        while True:
            rec = f.read(RECORD_SIZE)
            if len(rec) < RECORD_SIZE:
                break
            if rec[:4] != b'DREC':
                break

            result["records"] += 1

            # Распаковка
            _, recv_ts, sync_seq, sync_ts, delta_us, raw = RECORD_FMT.unpack(rec)

            # Монотонность ts
            if prev_ts is not None and recv_ts < prev_ts:
                result["ts_jumps"] += 1
            prev_ts = recv_ts

            # CRC
            crc_recv = struct.unpack_from('<H', raw, FRAME_SIZE - 2)[0]
            crc_calc = crc16_ccitt(raw[2:FRAME_SIZE - 2])
            if crc_recv == crc_calc:
                result["crc_ok"] += 1
            else:
                result["crc_err"] += 1

            # Sync
            if delta_us != UNSYNC_DELTA:
                result["synced"] += 1
            else:
                result["unsynced"] += 1

            # Потери пакетов (counter uint8)
            cnt = struct.unpack_from('B', raw, FRAME_SIZE - 4)[0]  # Counter
            if prev_cnt is not None:
                expected = (prev_cnt + 1) & 0xFF
                if cnt != expected:
                    result["pkt_loss"] += (cnt - expected) & 0xFF
            prev_cnt = cnt

    return result


def print_report(r, duration_s, rate_hz):
    total = r["records"]
    expected = int(duration_s * rate_hz)

    print()
    print("═" * 56)
    print("  Результаты loopback-теста")
    print("═" * 56)
    print("  Заголовок файла : {}".format(
        "✅ OK" if r.get("header_ok") else "❌ " + r.get("error", "?")))
    print("  Записей в файле : {:6d}  (ожидалось ≈ {})".format(total, expected))

    if total == 0:
        print("  ❌ Нет данных — проверь подключение")
        return

    capture_pct = total / expected * 100 if expected > 0 else 0
    crc_ok_pct  = r["crc_ok"] / total * 100
    sync_pct    = r["synced"] / total * 100

    print("  Захват данных   : {:5.1f}%  ({}/{})".format(
        capture_pct, total, expected))
    print("  CRC OK          : {:5.1f}%  ({}/{})".format(
        crc_ok_pct, r["crc_ok"], total))
    print("  Синхронизовано  : {:5.1f}%  ({}/{})".format(
        sync_pct, r["synced"], total))
    print("  Потери пакетов  : {}".format(r["pkt_loss"]))
    print("  Прыжки времени  : {}".format(r["ts_jumps"]))
    print("═" * 56)

    # Итоговый вердикт
    ok = (
        r.get("header_ok") and
        crc_ok_pct >= 99.0 and
        sync_pct   >= 95.0 and
        r["pkt_loss"] == 0 and
        r["ts_jumps"] == 0
    )
    if ok:
        print("  ✅ ТЕСТ ПРОЙДЕН — система работает корректно")
    else:
        print("  ❌ ТЕСТ НЕ ПРОЙДЕН — см. проблемы выше")
        if crc_ok_pct < 99.0:
            print("     → CRC ошибки: проверь протокол и SYNC_BYTES")
        if sync_pct < 95.0:
            print("     → Мало sync-меток: проверь SYNC_STORE и rate")
        if r["pkt_loss"] > 0:
            print("     → Потери пакетов: CPU перегружен или буфер мал")
        if r["ts_jumps"] > 0:
            print("     → Прыжки времени: проблема с WallClock")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def _main(args):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d  %(message)s",
        datefmt="%H:%M:%S",
    )

    print("═" * 56)
    print("  run_loopback_test.py  —  end-to-end loopback")
    print("  Python {}".format(sys.version.split()[0]))
    print("  Длительность: {} сек @ {} Гц".format(args.duration, args.rate))
    print("═" * 56)

    # ── Подготовка кадров ────────────────────────────────────
    if args.file and os.path.exists(args.file):
        frames = load_raw_frames(args.file)
        log.info("Загружено реальных кадров: %d из %s", len(frames), args.file)
    else:
        if args.file:
            log.warning("Файл '%s' не найден — генерируем синтетику", args.file)
        n = args.frames
        frames = [make_fake_frame(i, i * 0.0005) for i in range(n)]
        log.info("Синтетических кадров: %d", n)

    if not frames:
        log.error("Нет данных для теста")
        return

    # ── Запуск bench-сервера ──────────────────────────────────
    stop_evt  = threading.Event()
    ready_evt = threading.Event()

    bench_t = threading.Thread(
        target=bench_server_thread,
        args=(frames, args.rate, ready_evt, stop_evt),
        name="BenchServer",
        daemon=True,
    )
    bench_t.start()

    log.info("Ожидание bench-сервера...")
    ready_evt.wait(timeout=5.0)

    # ── Запуск sync-генератора ────────────────────────────────
    sync_t = threading.Thread(
        target=sync_gen_thread,
        args=(args.rate, stop_evt),
        name="SyncGen",
        daemon=True,
    )
    sync_t.start()

    # ── Disk writer ───────────────────────────────────────────
    stats  = Stats()
    writer = DiskWriter(OUTPUT_FILE)
    writer.start(b"")

    # ── Stats printer ─────────────────────────────────────────
    loop      = asyncio.get_event_loop()
    stat_task = loop.create_task(stat_printer(stats))

    # ── TCP receiver ──────────────────────────────────────────
    try:
        await tcp_receiver(writer, stats, args.duration)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Прерывание пользователем")
    finally:
        stop_evt.set()
        stat_task.cancel()
        bench_t.join(timeout=2.0)
        sync_t.join(timeout=2.0)
        writer.stop()

    # ── Валидация ─────────────────────────────────────────────
    log.info("Валидация %s ...", OUTPUT_FILE)
    result = validate_output(OUTPUT_FILE)
    print_report(result, args.duration, args.rate)


def main():
    p = argparse.ArgumentParser(description="Loopback end-to-end тест системы IMU")
    p.add_argument("--duration", type=float, default=10.0,
                   help="Длительность теста в секундах (default: 10)")
    p.add_argument("--rate",     type=int,   default=2000,
                   help="Частота пакетов Гц (default: 2000)")
    p.add_argument("--frames",   type=int,   default=8000,
                   help="Кол-во синтетических кадров (default: 8000, зациклится)")
    p.add_argument("--file",     default=None,
                   help="Путь к .bin файлу с реальными данными")
    args = p.parse_args()

    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
