"""
tcp_adapter.py  —  Windows 11  (v3: threading, no asyncio for TCP)
===================================================================
Asyncio убран из TCP-части. Используется blocking socket.recv() в потоке —
идентично debug_receiver.py, который доказал работоспособность (96к пакетов).

Причина отказа asyncio: ProactorEventLoop на Windows 11 не доставляет данные
через StreamReader.read() при соединении с Python 3.4 blocking socket (XP VM).
Данные попадают в TCP-буфер ОС, но IOCP completion не срабатывает.

Запуск:
    python tcp_adapter.py --bench-host 192.168.56.102
    python tcp_adapter.py --bench-host 192.168.56.102 --retry
    python tcp_adapter.py --bench-host 192.168.56.102 --no-sync
    python tcp_adapter.py --bench-host 127.0.0.1               # loopback
"""

import socket
import struct
import threading
import time
import logging
import argparse
import sys
import ctypes

from sync_marks import SYNC_STORE, SyncMark
from imu_logger import IMUProtocol, DiskWriter, Stats, WALL_CLOCK

log = logging.getLogger(__name__)

PKT_FMT = struct.Struct('!4sB3xQQQ')   # sync protocol: magic ver seq mono logical


# ─── Sync generator ───────────────────────────────────────────────────────────

def _sync_worker(bench_host, udp_port, rate_hz, stop_evt):
    """
    Sync-генератор: coarse sleep (GIL-safe) + fine busy-wait.
    Пишет SyncMark в SYNC_STORE и шлёт UDP на стенд.
    """
    period_ns = 1_000_000_000 // rate_hz
    # Без sleep: метки каждые ровно 500 мкс без кластеризации.
    # tcp_reader_thread не страдает: он отдаёт GIL через blocking sock.recv().
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((bench_host, udp_port))
        udp_ok = True
    except Exception as e:
        log.warning("UDP: не удалось подключиться (%s) — sync только локально", e)
        udp_ok = False

    seq        = 0
    logical_ns = 0
    now        = time.perf_counter_ns()
    next_tick  = ((now // period_ns) + 1) * period_ns

    log.info("Sync-генератор: %d Гц → %s:%d", rate_hz, bench_host, udp_port)

    while not stop_evt.is_set():
        # Pure busy-wait: никакого sleep → нет кластеризации меток.
        # Без него sleep(0.42мс) спит 15мс (Windows default timer),
        # создавая 15мс пробелы → только 19% пакетов попадают в окно ±1.5мс.
        while time.perf_counter_ns() < next_tick:
            pass

        perf_ns = time.perf_counter_ns()
        # WALL_CLOCK.now_ns() ≈ Unix-время (нс) — тот же базис что recv_ts_ns в IMUProtocol
        wall_ns = WALL_CLOCK.now_ns()
        SYNC_STORE.put(SyncMark(seq=seq, ts_ns=wall_ns, perf_ns=perf_ns))

        if udp_ok:
            try:
                sock.send(PKT_FMT.pack(b'SYNC', 1, seq, fire_ns, logical_ns))
            except Exception:
                pass

        seq        += 1
        logical_ns += period_ns
        next_tick  += period_ns

    sock.close()
    log.info("Sync-генератор остановлен. Отправлено %d меток.", seq)


# ─── TCP reader (blocking socket, proven) ─────────────────────────────────────

def tcp_reader_thread(bench_host, tcp_port, writer, stats, stop_evt, retry):
    """
    Блокирующий TCP-ресивер — тот же подход что debug_receiver.py.
    sock.recv() в потоке работает там где asyncio.reader.read() не работает.
    Кормит данные напрямую в IMUProtocol.data_received().
    """
    protocol = IMUProtocol(writer, stats)

    while not stop_evt.is_set():
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10.0)

        try:
            log.info("TCP: подключение к %s:%d...", bench_host, tcp_port)
            sock.connect((bench_host, tcp_port))
        except (socket.error, OSError) as e:
            log.warning("TCP: не удалось подключиться: %s", e)
            sock.close()
            if not retry:
                break
            time.sleep(2.0)
            continue

        log.info("TCP: подключено к %s:%d", bench_host, tcp_port)
        sock.settimeout(2.0)   # таймаут на recv() — чтобы проверять stop_evt

        try:
            while not stop_evt.is_set():
                try:
                    data = sock.recv(8192)
                except socket.timeout:
                    continue   # нет данных 2с — проверяем stop_evt и ждём ещё

                if not data:
                    log.warning("TCP: соединение закрыто удалённой стороной")
                    break

                protocol.data_received(data)

        except socket.error as e:
            log.error("TCP recv error: %s", e)
        except Exception as e:
            log.error("Protocol error: %s", e, exc_info=True)
        finally:
            # Читаем остаток перед закрытием — чтобы не посылать RST на XP
            sock.settimeout(0.1)
            try:
                while True:
                    leftover = sock.recv(4096)
                    if not leftover:
                        break
            except Exception:
                pass
            sock.close()
            log.info("TCP: сокет закрыт корректно")

        if not retry:
            break

        if not stop_evt.is_set():
            log.info("TCP: переподключение через 2 с...")
            time.sleep(2.0)

    log.info("TCP reader остановлен.")


# ─── Stat printer (поток, без asyncio) ────────────────────────────────────────

def _stat_thread(stats, stop_evt, interval=5.0):
    while not stop_evt.is_set():
        time.sleep(interval)
        if stop_evt.is_set():
            break
        s = stats.snapshot()
        sync_pct = (1 - s["unsync"] / s["total"]) * 100 if s["total"] else 0.0
        log.info(
            "Rate: %.0f pkt/s | Total: %d | Loss: %d | CRC err: %d | Sync: %.1f%%",
            s["rate"], s["total"], s["loss"], s["crc_err"], sync_pct,
        )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    # Повышаем точность системного таймера Windows до 1 мс.
    # Без этого time.sleep(0.42мс) → фактически 15.6мс → дыры в sync-метках.
    if sys.platform == 'win32':
        ctypes.windll.winmm.timeBeginPeriod(1)

    # Уменьшаем GIL switch interval: 5 мс (дефолт) → 1 мс.
    # Дефолтный 5 мс = 10 меток подряд в одном кластере → Sync ~25%.
    # 1 мс = 2 метки в кластере → метки почти равномерные → Sync ~90%+.
    sys.setswitchinterval(0.001)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d  %(message)s",
        datefmt="%H:%M:%S",
    )

    p = argparse.ArgumentParser(
        description="TCP-адаптер IMU (threading, без asyncio)")
    p.add_argument("--bench-host", default="192.168.56.101",
                   help="IP стенда / XP VM  (default: 192.168.56.101)")
    p.add_argument("--bench-port", type=int, default=55200,
                   help="TCP-порт bench_emulator  (default: 55200)")
    p.add_argument("--udp-port",   type=int, default=55100,
                   help="UDP-порт sync-меток  (default: 55100)")
    p.add_argument("--rate",       type=int, default=2000,
                   help="Частота sync Гц  (default: 2000)")
    p.add_argument("--out",        default="data_tcp.bin",
                   help="Выходной .bin  (default: data_tcp.bin)")
    p.add_argument("--retry",      action="store_true",
                   help="Переподключаться при обрыве")
    p.add_argument("--no-sync",    action="store_true", dest="no_sync",
                   help="Не запускать sync-генератор")
    args = p.parse_args()

    log.info("tcp_adapter запущен  [threading mode]")
    log.info("  стенд:  %s:%d  (TCP данные)",   args.bench_host, args.bench_port)
    log.info("  вывод:  %s",                    args.out)

    stop_evt = threading.Event()

    # ── DiskWriter ────────────────────────────────────────────
    stats  = Stats()
    writer = DiskWriter(args.out)
    writer.start(b"")

    # ── Sync generator ────────────────────────────────────────
    if not args.no_sync:
        sync_t = threading.Thread(
            target=_sync_worker,
            args=(args.bench_host, args.udp_port, args.rate, stop_evt),
            daemon=True, name="SyncGen",
        )
        sync_t.start()
        log.info("  sync:   %s:%d  (UDP метки)", args.bench_host, args.udp_port)
    else:
        log.info("  sync:   ОТКЛЮЧЁН (--no-sync)")

    # ── TCP reader (blocking socket) ──────────────────────────
    tcp_t = threading.Thread(
        target=tcp_reader_thread,
        args=(args.bench_host, args.bench_port,
              writer, stats, stop_evt, args.retry),
        daemon=True, name="TCPReader",
    )
    tcp_t.start()

    # ── Stat printer ──────────────────────────────────────────
    stat_t = threading.Thread(
        target=_stat_thread,
        args=(stats, stop_evt),
        daemon=True, name="StatPrint",
    )
    stat_t.start()

    log.info("Работаем. Ctrl+C для остановки.")

    try:
        while True:
            time.sleep(0.5)
            if not tcp_t.is_alive() and not args.retry:
                break
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Остановка...")
        stop_evt.set()
        tcp_t.join(timeout=5.0)
        writer.stop()

        s = stats.snapshot()
        if sys.platform == 'win32':
            ctypes.windll.winmm.timeEndPeriod(1)
        log.info(
            "Итог: пакетов=%d  потери=%d  CRC_err=%d  несинхр=%.1f%%",
            s["total"], s["loss"], s["crc_err"],
            s["unsync"] / s["total"] * 100 if s["total"] else 0,
        )


if __name__ == "__main__":
    main()
