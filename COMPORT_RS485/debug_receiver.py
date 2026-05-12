"""
debug_receiver.py  —  Windows 11
==================================
Минимальный диагностический скрипт.
Подключается к bench_emulator, читает сырые байты,
ищет C0C0, проверяет CRC и считает валидные пакеты.

НЕ использует asyncio, imu_logger, SYNC_STORE — ничего лишнего.
Цель: убедиться что TCP-данные доходят и парсятся.

Запуск:
    python debug_receiver.py --host 192.168.56.102
    python debug_receiver.py --host 127.0.0.1        # loopback
    python debug_receiver.py --host 192.168.56.102 --dump 5
"""

import socket
import struct
import time
import sys
import argparse

FRAME_SIZE  = 32
SYNC_BYTES  = b'\xc0\xc0'

def crc16_ccitt(data, poly=0x1021, init=0xFFFF):
    crc = init
    for b in bytearray(data):
        crc ^= (b << 8)
        for _ in range(8):
            crc = ((crc << 1) ^ poly if crc & 0x8000 else crc << 1) & 0xFFFF
    return crc

def parse_frames(buf, stats):
    """Ищет C0C0 → проверяет CRC → считает пакеты. Возвращает остаток буфера."""
    while True:
        idx = buf.find(SYNC_BYTES)
        if idx == -1:
            stats['no_sync'] += len(buf)
            return buf[-1:] if buf else b''

        if idx > 0:
            stats['skipped'] += idx
            buf = buf[idx:]

        if len(buf) < FRAME_SIZE:
            return buf

        frame = buf[:FRAME_SIZE]
        crc_recv = struct.unpack_from('<H', frame, FRAME_SIZE - 2)[0]
        crc_calc = crc16_ccitt(frame[2:FRAME_SIZE - 2])

        if crc_recv != crc_calc:
            stats['crc_fail'] += 1
            buf = buf[1:]          # сдвиг на 1 → следующий поиск C0C0
            continue

        stats['ok'] += 1
        buf = buf[FRAME_SIZE:]

def main():
    p = argparse.ArgumentParser(description='Диагностика TCP-потока от bench_emulator')
    p.add_argument('--host', default='192.168.56.102', help='IP стенда / XP VM')
    p.add_argument('--port', type=int, default=55200)
    p.add_argument('--dump', type=int, default=3,
                   help='Напечатать первые N валидных пакетов (default: 3)')
    p.add_argument('--timeout', type=float, default=30.0,
                   help='Остановиться через N секунд')
    args = p.parse_args()

    print('=' * 56)
    print('  debug_receiver.py')
    print('  Подключение к {}:{} ...'.format(args.host, args.port))
    print('=' * 56)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10.0)

    try:
        sock.connect((args.host, args.port))
    except socket.error as e:
        print('[ERR] Не удалось подключиться: {}'.format(e))
        sys.exit(1)

    sock.settimeout(2.0)
    print('[OK] Подключено. Читаем {}с...'.format(int(args.timeout)))
    print()

    buf   = b''
    stats = {'ok': 0, 'crc_fail': 0, 'skipped': 0, 'no_sync': 0, 'raw_bytes': 0}
    dump_count = 0

    t_start    = time.time()
    t_last_rep = t_start

    try:
        while True:
            elapsed = time.time() - t_start
            if elapsed >= args.timeout:
                break

            # Читаем данные
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                # Нет данных 2 секунды — печатаем промежуточный отчёт
                now = time.time()
                print('[WARN] Нет данных уже {:.1f}с'.format(now - t_last_rep))
                t_last_rep = now
                continue
            except socket.error as e:
                print('[ERR] recv: {}'.format(e))
                break

            if not chunk:
                print('[WARN] Соединение закрыто удалённой стороной')
                break

            stats['raw_bytes'] += len(chunk)
            buf += chunk

            # ── Первые 16 байт для диагностики ──────────────────
            if stats['raw_bytes'] <= len(chunk) and stats['raw_bytes'] <= 64:
                print('[DEBUG] Первые байты: {}'.format(chunk[:32].hex(' ')))
                print('[DEBUG] Ищем C0C0: {}'.format(
                    'найден на pos {}'.format(chunk.find(SYNC_BYTES))
                    if SYNC_BYTES in chunk else 'НЕ НАЙДЕН в первом чанке!'))
                print()

            # ── Разбор пакетов ───────────────────────────────────
            before = stats['ok']
            buf = parse_frames(buf, stats)
            new_ok = stats['ok'] - before

            # Печатаем первые N валидных пакетов
            if dump_count < args.dump and new_ok > 0:
                # Повторно распакуем последний валидный пакет
                # (buf уже сдвинут, ищем в чанке)
                tmp = chunk
                tmp_idx = tmp.find(SYNC_BYTES)
                while tmp_idx != -1 and dump_count < args.dump:
                    frame_candidate = tmp[tmp_idx:tmp_idx + FRAME_SIZE]
                    if len(frame_candidate) == FRAME_SIZE:
                        crc_r = struct.unpack_from('<H', frame_candidate, 30)[0]
                        crc_c = crc16_ccitt(frame_candidate[2:30])
                        if crc_r == crc_c:
                            fields = struct.unpack('<H6ih2BH', frame_candidate)
                            hdr, gx, gy, gz, ax, ay, az, tc, cnt, st, crc = fields
                            print('[PKT #{}] hdr=0x{:04X} Gx={} Gy={} Gz={} '
                                  'Ax={} Ay={} Az={} TC={} Cnt={} CRC=OK'.format(
                                      stats['ok'], hdr, gx, gy, gz, ax, ay, az,
                                      tc, cnt))
                            dump_count += 1
                    tmp = tmp[tmp_idx + 1:]
                    tmp_idx = tmp.find(SYNC_BYTES)

            # Отчёт каждые 5 секунды
            now = time.time()
            if now - t_last_rep >= 5.0:
                rate = stats['ok'] / (now - t_start)
                print('[STAT] raw={} bytes  ok={} pkt  crc_fail={}  '
                      'skipped={} bytes  rate={:.0f} pkt/s'.format(
                          stats['raw_bytes'], stats['ok'],
                          stats['crc_fail'], stats['skipped'], rate))
                t_last_rep = now

    except KeyboardInterrupt:
        print('\n[STOP] Прерывание пользователем')
    finally:
        sock.close()

    # ── Финальный отчёт ──────────────────────────────────────────
    elapsed = time.time() - t_start
    print()
    print('=' * 56)
    print('  Результаты диагностики ({:.1f}с)'.format(elapsed))
    print('=' * 56)
    print('  Получено байт    : {:>10,}'.format(stats['raw_bytes']))
    print('  Валидных пакетов : {:>10,}'.format(stats['ok']))
    print('  CRC ошибок       : {:>10,}'.format(stats['crc_fail']))
    print('  Пропущено байт   : {:>10,}'.format(stats['skipped']))

    if stats['raw_bytes'] == 0:
        print()
        print('  ДИАГНОЗ: данные НЕ поступают по TCP')
        print('  → проверь что bench_emulator запущен')
        print('  → проверь IP, порт, firewall на XP VM')
    elif stats['ok'] == 0 and stats['crc_fail'] == 0:
        print()
        print('  ДИАГНОЗ: данные есть, но C0C0 не найден!')
        print('  → скорее всего SYNC_BYTES не совпадает')
        print('  → проверь реальный заголовок пакетов в data.bin')
    elif stats['ok'] == 0 and stats['crc_fail'] > 0:
        print()
        print('  ДИАГНОЗ: C0C0 найден, но CRC всегда неверен')
        print('  → возможно формат data.bin другой (ts+frame = 40B?)')
        print('  → попробуй запустить с --fake флагом на bench_emulator')
    else:
        rate = stats['ok'] / elapsed
        print('  Rate             : {:>10.1f} pkt/s'.format(rate))
        print()
        print('  TCP и парсинг работают корректно!')
        print('  Проблема в tcp_adapter.py или imu_logger.py')
    print('=' * 56)


if __name__ == '__main__':
    main()