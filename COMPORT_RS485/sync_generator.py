"""
sync_generator.py  (Windows 11 edition)
Генератор синхроимпульсов 2кГц для Windows 11.
Использует CreateWaitableTimerEx HIGH_RESOLUTION вместо SCHED_FIFO.
"""

import os
import sys
import time
import socket
import struct
import ctypes
import ctypes.wintypes
import threading
import ntplib
import logging
from sync_marks import SYNC_STORE, SyncMark

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d  %(message)s",
    datefmt="%H:%M:%S"
)

# ═══════════════════════════ НАСТРОЙКИ ═══════════════════════════
BENCH_IP          = "192.168.1.100"
BENCH_UDP_PORT    = 55100
NTP_SERVER        = "pool.ntp.org"
PULSE_HZ          = 2000
PULSE_INTERVAL_NS = 1_000_000_000 // PULSE_HZ   # 500 000 нс

# Сколько мкс до цели переходим на busy-wait (подстраховка)
SPINLOCK_LEAD_NS  = 150_000    # 150 мкс

STATS_INTERVAL    = 2000
# ═════════════════════════════════════════════════════════════════

PKT_FMT  = "!4sIQ"
PKT_SIZE = struct.calcsize(PKT_FMT)   # 16 байт


# ─── Windows API ─────────────────────────────────────────────────
class WindowsTimer:
    """
    Обёртка над CreateWaitableTimerEx с флагом HIGH_RESOLUTION.
    Доступен на Windows 10 v2004+ и Windows 11.
    Даёт точность ~100–300 мкс без 100% загрузки CPU.
    """
    CREATE_WAITABLE_TIMER_HIGH_RESOLUTION = 0x00000002
    TIMER_ALL_ACCESS = 0x1F0003

    def __init__(self):
        kernel32 = ctypes.windll.kernel32

        # Пробуем создать high-resolution таймер
        self._handle = kernel32.CreateWaitableTimerExW(
            None,           # security attributes
            None,           # name
            self.CREATE_WAITABLE_TIMER_HIGH_RESOLUTION,
            self.TIMER_ALL_ACCESS
        )
        if not self._handle:
            # Fallback: обычный таймер (Windows 7/8/10 < 2004)
            log.warning(
                "HIGH_RESOLUTION таймер недоступен, "
                "используем обычный WaitableTimer. "
                "Точность снизится до ~1–2 мс."
            )
            self._handle = kernel32.CreateWaitableTimerW(
                None, True, None
            )
        else:
            log.info("High-Resolution WaitableTimer создан.")

        self._kernel32 = kernel32

    def wait_until_ns(self, target_ns: int):
        """
        Ждать до target_ns (в единицах time.perf_counter_ns).
        Использует таймер для крупного ожидания,
        затем busy-wait для финальных мкс.
        """
        now = time.perf_counter_ns()
        wait_ns = target_ns - now - SPINLOCK_LEAD_NS

        if wait_ns > 0:
            # Windows FILETIME: отрицательное значение = относительное,
            # единица = 100 нс
            due_time = ctypes.c_longlong(-wait_ns // 100)
            self._kernel32.SetWaitableTimer(
                self._handle,
                ctypes.byref(due_time),
                0,      # period (0 = one-shot)
                None, None, False
            )
            self._kernel32.WaitForSingleObject(self._handle, 0xFFFFFFFF)

        # Финальный busy-wait
        while time.perf_counter_ns() < target_ns:
            pass

    def close(self):
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _boost_windows_priority():
    """
    Поднять приоритет процесса и потока до максимума на Windows.
    REALTIME_PRIORITY_CLASS + THREAD_PRIORITY_TIME_CRITICAL.
    """
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        process = kernel32.GetCurrentProcess()
        thread  = kernel32.GetCurrentThread()

        REALTIME_PRIORITY_CLASS    = 0x00000100
        THREAD_PRIORITY_TIME_CRITICAL = 15

        ok_p = kernel32.SetPriorityClass(process, REALTIME_PRIORITY_CLASS)
        ok_t = kernel32.SetThreadPriority(thread, THREAD_PRIORITY_TIME_CRITICAL)

        if ok_p and ok_t:
            log.info("Приоритет: REALTIME_PRIORITY_CLASS + TIME_CRITICAL.")
        else:
            log.warning(
                "Не удалось установить RT-приоритет "
                "(SetPriorityClass=%d, SetThreadPriority=%d). "
                "Запустите от имени Администратора.", ok_p, ok_t
            )
    except Exception as e:
        log.warning("boost_priority: %s", e)


def _disable_power_throttling():
    """
    Отключить троттлинг производительности для процесса (Windows 11).
    Без этого ОС может снижать частоту CPU в «тихих» потоках.
    """
    if sys.platform != "win32":
        return
    try:
        PROCESS_INFORMATION_CLASS_POWER_THROTTLING = 4
        PROCESS_POWER_THROTTLING_CURRENT_VERSION   = 1
        PROCESS_POWER_THROTTLING_EXECUTION_SPEED   = 0x1

        class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
            _fields_ = [
                ("Version",    ctypes.c_ulong),
                ("ControlMask",ctypes.c_ulong),
                ("StateMask",  ctypes.c_ulong),
            ]

        state = PROCESS_POWER_THROTTLING_STATE(
            Version=PROCESS_POWER_THROTTLING_CURRENT_VERSION,
            ControlMask=PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
            StateMask=0   # 0 = отключить троттлинг
        )
        ctypes.windll.kernel32.SetProcessInformation(
            ctypes.windll.kernel32.GetCurrentProcess(),
            PROCESS_INFORMATION_CLASS_POWER_THROTTLING,
            ctypes.byref(state),
            ctypes.sizeof(state)
        )
        log.info("Power throttling отключён.")
    except Exception as e:
        log.warning("disable_power_throttling: %s", e)


def _pin_to_cpu_core(core: int = 2):
    """
    Привязать поток к конкретному ядру CPU.
    Снижает jitter от миграции потока между ядрами.
    core=2 → не трогаем ядро 0 (системные прерывания) и 1.
    """
    if sys.platform != "win32":
        return
    try:
        mask = 1 << core
        thread = ctypes.windll.kernel32.GetCurrentThread()
        ctypes.windll.kernel32.SetThreadAffinityMask(thread, mask)
        log.info("Поток генератора привязан к CPU core %d.", core)
    except Exception as e:
        log.warning("pin_to_core: %s", e)


# ─── NTP ─────────────────────────────────────────────────────────
class NTPClock:
    def __init__(self, server: str):
        self.server   = server
        self._offset_ns = 0
        self._lock    = threading.Lock()

    def sync(self):
        c = ntplib.NTPClient()
        r = c.request(self.server, version=3)
        with self._lock:
            self._offset_ns = int(r.offset * 1e9)
        log.info("NTP offset: %+.3f мс  stratum=%d", r.offset * 1000, r.stratum)

    def now_ns(self) -> int:
        with self._lock:
            return time.time_ns() + self._offset_ns

    def resync_loop(self, interval_s: float = 30.0):
        while True:
            try:
                self.sync()
            except Exception as e:
                log.warning("NTP resync: %s", e)
            time.sleep(interval_s)


# ─── Основной цикл ───────────────────────────────────────────────
def generator_loop(ntp: NTPClock):
    _boost_windows_priority()
    _disable_power_throttling()
    _pin_to_cpu_core(core=2)

    timer = WindowsTimer()
    sock  = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.connect((BENCH_IP, BENCH_UDP_PORT))

    # Увеличить буфер отправки (снижает риск потери при burst)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)

    seq = 0
    jitter_sum = 0.0
    jitter_max = 0.0

    # Выровнять старт на границу интервала
    now_perf = time.perf_counter_ns()
    now_ntp  = ntp.now_ns()

    # perf_counter не совпадает с time_ns по абсолюту,
    # запоминаем смещение при старте
    perf_to_ntp_offset = now_ntp - now_perf

    def perf_to_ntp(perf_ns: int) -> int:
        return perf_ns + perf_to_ntp_offset

    next_fire_perf = (
        (now_perf // PULSE_INTERVAL_NS) + 1
    ) * PULSE_INTERVAL_NS

    log.info(
        "Генератор запущен: %d Гц, интервал=%d нс",
        PULSE_HZ, PULSE_INTERVAL_NS
    )

    try:
        while True:
            # ── Ждать до момента срабатывания ───────────────────
            timer.wait_until_ns(next_fire_perf)
            fire_perf_ns = time.perf_counter_ns()
            fire_ntp_ns  = perf_to_ntp(fire_perf_ns)

            # ── Отправить UDP ────────────────────────────────────
            pkt = struct.pack(PKT_FMT, b"SYNC", seq, fire_ntp_ns)
            try:
                sock.send(pkt)
            except OSError as e:
                log.error("UDP send: %s", e)

            # ── Записать метку для логгера ───────────────────────
            SYNC_STORE.put(SyncMark(
                seq=seq,
                ts_ns=fire_ntp_ns,
                perf_ns=fire_perf_ns
            ))

            # ── Статистика jitter ────────────────────────────────
            err_ns = abs(fire_perf_ns - next_fire_perf)
            jitter_sum += err_ns
            if err_ns > jitter_max:
                jitter_max = err_ns

            if seq > 0 and seq % STATS_INTERVAL == 0:
                log.info(
                    "seq=%d  jitter_avg=%.1f мкс  jitter_max=%.1f мкс",
                    seq,
                    jitter_sum / STATS_INTERVAL / 1e3,
                    jitter_max / 1e3
                )
                jitter_sum = 0.0
                jitter_max = 0.0

            seq += 1
            next_fire_perf += PULSE_INTERVAL_NS

    except KeyboardInterrupt:
        pass
    finally:
        timer.close()
        sock.close()
        log.info("Генератор остановлен. Отправлено %d импульсов.", seq)


def run():
    ntp = NTPClock(NTP_SERVER)
    try:
        ntp.sync()
    except Exception as e:
        log.error("NTP: %s", e)

    threading.Thread(target=ntp.resync_loop, daemon=True).start()

    gen = threading.Thread(
        target=generator_loop, args=(ntp,), daemon=False, name="SyncGen"
    )
    gen.start()
    gen.join()


if __name__ == "__main__":
    run()