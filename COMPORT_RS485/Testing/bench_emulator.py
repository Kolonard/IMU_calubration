"""
bench_emulator.py
=================
Python 3.4+ (Windows XP VM)

Эмулирует стенд:
  - Читает пакеты из data.bin (реальные данные) или генерирует синтетику
  - Отправляет 32-байтные пакеты по TCP на ПК записи
  - Принимает UDP sync-метки от sync_generator (для проверки связи)

Запуск:
  python bench_emulator.py                        # синтетика
  python bench_emulator.py --file data.bin        # реальные данные
  python bench_emulator.py --file data.bin --loop # зациклить данные
  python bench_emulator.py --host 192.168.1.10    # IP ПК записи (не нужен для TCP-сервера)

Аргументы:
  --file FILE     путь к .bin файлу с данными (raw 32B или наш формат 58B)
  --port PORT     TCP-порт сервера (default: 55200)
  --sync-port P   UDP-порт для приёма sync-меток (default: 55100)
  --rate HZ       частота отправки пакетов (default: 2000)
  --loop          зациклить данные из файла
  --no-sync       не слушать UDP sync
"""

from __future__ import print_function

import sys
import os
import socket
import struct
import threading
import time
import math
import argparse
import ctypes

# ═══════════════════════ НАСТРОЙКИ ══════════════════════
FRAME_SIZE     = 32
SYNC_HEADER    = b'\xc0\xc0'

# Формат пакета
PACKET_STRUCT = struct.Struct('<H6ih2BH')  # 32 байта

# Формат нашего .bin файла (для авто-детекции)
BIN_MAGIC      = b'IMULOG01'
BIN_HDR_SIZE   = 64
BIN_REC_SIZE   = 58
BIN_REC_FMT    = struct.Struct('<4sQIqh32s')  # DREC + ts + seq + ts2 + delta + raw

# Статистика: раз в N секунд
STAT_INTERVAL  = 5.0
# ════════════════════════════════════════════════════════


# ─── CRC ──────────────────────────────────────────────────────────────────────

def crc16_ccitt(data, poly=0x1021, init=0xFFFF):
    crc = init
    for b in bytearray(data):
        crc ^= (b << 8)
        for _ in range(8):
            crc = ((crc << 1) ^ poly if crc & 0x8000 else crc << 1) & 0xFFFF
    return crc


# ─── Загрузка реальных данных ─────────────────────────────────────────────────

def load_frames_from_file(path, loop=False):
    """
    Читает 32-байтные пакеты из файла.
    Поддерживает два формата:
      1. Raw: просто 32-байтные кадры подряд
      2. Наш .bin: заголовок 64B + записи по 58B (извлекаем поле raw[32])
    Возвращает list[bytes].
    """
    frames = []

    with open(path, 'rb') as f:
        magic = f.read(8)

        if magic == BIN_MAGIC:
            # Наш формат imu_logger.py
            print("[INFO] Формат файла: IMULOG01 (58-байтные записи)")
            f.seek(BIN_HDR_SIZE)
            while True:
                rec = f.read(BIN_REC_SIZE)
                if len(rec) < BIN_REC_SIZE:
                    break
                if rec[:4] != b'DREC':
                    print("[WARN] Неверный маркер записи, пропускаем")
                    continue
                # raw frame — последние 32 байта записи
                raw = rec[BIN_REC_SIZE - FRAME_SIZE:]
                frames.append(raw)
        else:
            # Raw формат: просто 32-байтные кадры
            print("[INFO] Формат файла: raw 32-байтные кадры")
            f.seek(0)
            while True:
                frame = f.read(FRAME_SIZE)
                if len(frame) < FRAME_SIZE:
                    break
                frames.append(frame)

    print("[INFO] Загружено кадров: {0}".format(len(frames)))
    return frames


def validate_frames(frames):
    """Проверяет CRC и заголовок. Возвращает (ok, errors)."""
    ok = 0
    errors = 0
    for f in frames:
        if len(f) != FRAME_SIZE:
            errors += 1
            continue
        header = struct.unpack_from('<H', f, 0)[0]
        if header != 0xC0C0:
            errors += 1
            continue
        crc_recv = struct.unpack_from('<H', f, FRAME_SIZE - 2)[0]
        crc_calc = crc16_ccitt(f[2:FRAME_SIZE - 2])
        if crc_recv == crc_calc:
            ok += 1
        else:
            errors += 1
    return ok, errors


# ─── Синтетические данные ─────────────────────────────────────────────────────

def make_fake_frame(counter, t):
    """
    Генерирует синтетический 32-байтный IMU-пакет с валидным CRC.
    Данные: синусоидальные значения гироскопа и акселерометра.
    """
    gx = int(math.sin(t * 2.0) * 10000)
    gy = int(math.cos(t * 3.0) * 8000)
    gz = int(math.sin(t * 1.5) * 6000)
    ax = int(math.cos(t * 0.5) * 50000)
    ay = int(math.sin(t * 0.7) * 40000)
    az = 98000 + int(math.cos(t * 1.1) * 2000)  # ~9.8 g
    tc = 2500     # 25.00 °C
    cnt = counter & 0xFF
    status = 0

    # Формат: header(2) + Gx,Gy,Gz,Ax,Ay,Az(6×4) + TC(2) + Cnt,St(2) = 30 байт
    payload = struct.pack('<H6ih2B',
                          0xC0C0, gx, gy, gz, ax, ay, az, tc, cnt, status)
    crc = crc16_ccitt(payload[2:])   # CRC покрывает всё кроме заголовка
    return payload + struct.pack('<H', crc)


def generate_fake_frames(count):
    """Создаёт N синтетических кадров."""
    frames = []
    for i in range(count):
        t = i * 0.0005   # 0.5 мс / кадр
        frames.append(make_fake_frame(i, t))
    print("[INFO] Сгенерировано синтетических кадров: {0}".format(count))
    return frames


# ─── Таймер ───────────────────────────────────────────────────────────────────

def setup_timer_resolution():
    """Устанавливает разрешение таймера 1мс на Windows."""
    if sys.platform == 'win32':
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)
            print("[OK] Разрешение таймера: 1 мс")
        except Exception as e:
            print("[WARN] timeBeginPeriod: {0}".format(e))


def sleep_until(target_perf):
    """Ждать до target_perf (time.perf_counter()). Грубый sleep + busy-wait."""
    remaining = target_perf - time.perf_counter()
    if remaining > 0.001:
        time.sleep(remaining - 0.001)
    while time.perf_counter() < target_perf:
        pass


# ─── UDP: приём sync-меток ────────────────────────────────────────────────────

class SyncListener(object):
    """Слушает UDP-порт, считает принятые sync-метки."""
    PKT_FMT = struct.Struct('!4sB3xQQQ')  # из sync_protocol.py
    MAGIC = b'SYNC'

    def __init__(self, port):
        self.port = port
        self.count = 0
        self.last_seq = None
        self.lost = 0
        self._stop = threading.Event()

    def start(self):
        t = threading.Thread(target=self._run, name='SyncUDP')
        t.daemon = True
        t.start()

    def _run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('0.0.0.0', self.port))
        except socket.error as e:
            print("[WARN] UDP bind failed: {0}".format(e))
            return
        sock.settimeout(1.0)
        print("[OK] UDP sync listener: порт {0}".format(self.port))

        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(64)
            except socket.timeout:
                continue
            except Exception:
                break

            if len(data) < self.PKT_FMT.size:
                continue
            try:
                parts = self.PKT_FMT.unpack(data[:self.PKT_FMT.size])
                magic, version, seq = parts[0], parts[1], parts[2]
            except struct.error:
                continue

            if magic != self.MAGIC:
                continue

            if self.last_seq is not None and seq != self.last_seq + 1:
                self.lost += (seq - self.last_seq - 1) & 0xFFFFFFFF
            self.last_seq = seq
            self.count += 1

        sock.close()

    def stop(self):
        self._stop.set()

    def stats(self):
        return self.count, self.lost


# ─── TCP: отправка данных ─────────────────────────────────────────────────────

class TCPSender(object):
    """
    TCP-сервер: ждёт подключения от tcp_adapter.py / imu_logger,
    затем отправляет кадры с заданной частотой.
    """

    def __init__(self, bind_port, frames, rate_hz, loop_frames):
        self.bind_port  = bind_port
        self.frames     = frames
        self.rate_hz    = rate_hz
        self.loop       = loop_frames
        self.period     = 1.0 / rate_hz
        self._stop      = threading.Event()

        # Статистика
        self.sent       = 0
        self.overruns   = 0
        self._lock      = threading.Lock()

    def run(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('0.0.0.0', self.bind_port))
        srv.listen(1)
        print("[OK] TCP сервер слушает: 0.0.0.0:{0}".format(self.bind_port))
        print("[..] Ожидание подключения ПК записи...")

        while not self._stop.is_set():
            srv.settimeout(2.0)
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue

            print("[OK] Подключился: {0}:{1}".format(addr[0], addr[1]))
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)

            try:
                self._send_loop(conn)
            except (socket.error, OSError) as e:
                print("[WARN] Соединение разорвано: {0}".format(e))
            finally:
                conn.close()
                print("[..] Клиент отключился. Ожидание нового подключения...")

        srv.close()

    def _send_loop(self, conn):
        frames  = self.frames
        n       = len(frames)
        period  = self.period
        idx     = 0
        counter = 0

        OVERRUN_THRESHOLD = period * 3

        next_tick = time.perf_counter()
        last_stat = time.perf_counter()

        while not self._stop.is_set():
            next_tick += period
            now = time.perf_counter()

            # Защита от накопленного отставания
            lateness = now - next_tick
            if lateness > OVERRUN_THRESHOLD:
                with self._lock:
                    self.overruns += 1
                next_tick = now + period

            sleep_until(next_tick)

            frame = frames[idx]
            try:
                conn.sendall(frame)
            except socket.error:
                raise

            with self._lock:
                self.sent += 1

            idx += 1
            if idx >= n:
                if self.loop:
                    idx = 0
                else:
                    print("\n[OK] Все кадры отправлены ({0}).".format(n))
                    break
            counter += 1

            # Статистика
            now2 = time.perf_counter()
            if now2 - last_stat >= STAT_INTERVAL:
                with self._lock:
                    s = self.sent
                    o = self.overruns
                elapsed = now2 - last_stat
                rate = STAT_INTERVAL / period
                print("[STAT] Отправлено: {0:6d} пакетов | "
                      "Rate: ~{1:.0f} pkt/s | "
                      "Overrun: {2}".format(s, rate, o))
                last_stat = now2

    def stop(self):
        self._stop.set()


# ─── Статистика в отдельном потоке ────────────────────────────────────────────

def stats_thread(sender, sync_listener, interval):
    while True:
        time.sleep(interval)
        with sender._lock:
            sent    = sender.sent
            overrun = sender.overruns
        sync_count, sync_lost = sync_listener.stats()
        print("[STAT] TCP sent: {0:7d} | overrun: {1} | "
              "UDP sync recv: {2:6d} | sync lost: {3}".format(
                  sent, overrun, sync_count, sync_lost))


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Эмулятор стенда IMU (Python 3.4+)')
    parser.add_argument('--file',      default='data.bin',
                        help='Путь к .bin файлу (default: data.bin)')
    parser.add_argument('--port',      type=int, default=55200,
                        help='TCP-порт сервера (default: 55200)')
    parser.add_argument('--sync-port', type=int, default=55100,
                        dest='sync_port',
                        help='UDP-порт sync-меток (default: 55100)')
    parser.add_argument('--rate',      type=int, default=2000,
                        help='Частота кадров (default: 2000 Гц)')
    parser.add_argument('--loop',      action='store_true',
                        help='Зациклить данные из файла')
    parser.add_argument('--no-sync',   action='store_true', dest='no_sync',
                        help='Не слушать UDP sync')
    parser.add_argument('--fake',      type=int, default=0,
                        metavar='N',
                        help='Генерировать N синтетических кадров (игнорирует --file)')
    args = parser.parse_args()

    print("=" * 56)
    print("  bench_emulator.py  —  эмулятор стенда IMU")
    print("  Python {0}".format(sys.version.split()[0]))
    print("=" * 56)

    setup_timer_resolution()

    # ── Загрузка данных ──────────────────────────────────
    if args.fake > 0:
        frames = generate_fake_frames(args.fake)
        loop_frames = args.loop
    elif os.path.exists(args.file):
        frames = load_frames_from_file(args.file, loop=args.loop)
        ok, errors = validate_frames(frames)
        print("[INFO] Валидация CRC: ok={0}, errors={1}".format(ok, errors))
        loop_frames = args.loop
    else:
        print("[WARN] Файл '{0}' не найден. "
              "Генерируем 10000 синтетических кадров.".format(args.file))
        frames = generate_fake_frames(10000)
        loop_frames = True

    if not frames:
        print("[ERR] Нет данных для отправки.")
        sys.exit(1)

    duration_s = len(frames) / float(args.rate)
    if not loop_frames:
        print("[INFO] Данные: {0} кадров ~= {1:.1f} сек @ {2} Гц".format(
            len(frames), duration_s, args.rate))
    else:
        print("[INFO] Данные: {0} кадров (зациклено) @ {1} Гц".format(
            len(frames), args.rate))

    # ── UDP sync listener ────────────────────────────────
    sync = SyncListener(args.sync_port)
    if not args.no_sync:
        sync.start()

    # ── TCP sender ───────────────────────────────────────
    sender = TCPSender(
        bind_port=args.port,
        frames=frames,
        rate_hz=args.rate,
        loop_frames=loop_frames,
    )

    # Статистика в фоне
    st = threading.Thread(
        target=stats_thread, args=(sender, sync, STAT_INTERVAL))
    st.daemon = True
    st.start()

    print("\n[START] Нажми Ctrl+C для остановки.\n")
    try:
        sender.run()   # блокирует
    except KeyboardInterrupt:
        print("\n[STOP] Остановка...")
    finally:
        sender.stop()
        sync.stop()
        s, l = sync.stats()
        print("[FINAL] Отправлено TCP: {0} | "
              "Принято UDP sync: {1} | Потеряно sync: {2}".format(
                  sender.sent, s, l))


if __name__ == '__main__':
    main()
