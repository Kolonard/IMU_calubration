"""
measure_jitter.py
=================
Запускай прямо на боевом ПК (Windows 11) перед основным запуском.
Измеряет реальный jitter таймера без UDP, выводит гистограмму в консоль.

Использование:
  python measure_jitter.py [--hz 2000] [--seconds 10] [--timer {hires|sleep|spin}]
  python measure_jitter.py --compare    # сравнить все три таймера

Интерпретация:
  avg_jitter < 150 мкс  → отлично
  avg_jitter < 350 мкс  → приемлемо для 2кГц
  avg_jitter > 500 мкс  → проблема, проверь приоритет/affinity
  p99_jitter > 1000 мкс → будут пропуски импульсов
"""

import argparse
import ctypes
import sys
import time
import statistics
import collections


def setup_windows_rt():
    if sys.platform != "win32":
        print("[WARN] Не Windows — RT настройки недоступны.")
        return

    k32 = ctypes.windll.kernel32
    k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00000080)
    k32.SetThreadPriority(k32.GetCurrentThread(), 2)

    cpu_count = __import__("multiprocessing").cpu_count()
    core = min(2, cpu_count - 1)
    k32.SetThreadAffinityMask(k32.GetCurrentThread(), 1 << core)

    try:
        class PTS(ctypes.Structure):
            _fields_ = [("Version", ctypes.c_ulong),
                        ("ControlMask", ctypes.c_ulong),
                        ("StateMask", ctypes.c_ulong)]
        state = PTS(Version=1, ControlMask=0x1, StateMask=0)
        k32.SetProcessInformation(
            k32.GetCurrentProcess(), 4,
            ctypes.byref(state), ctypes.sizeof(state)
        )
    except Exception:
        pass

    print(f"[OK] RT настройки применены (core {core}).")


def make_hires_timer():
    k32 = ctypes.windll.kernel32
    handle = k32.CreateWaitableTimerExW(None, None, 0x00000002, 0x1F0003)
    if not handle:
        print("[WARN] HIGH_RESOLUTION таймер недоступен, fallback.")
        handle = k32.CreateWaitableTimerW(None, True, None)

    SPIN_NS = 80_000

    def wait_until(target_ns):
        while True:
            now = time.perf_counter_ns()
            rem = target_ns - now
            if rem <= 0:
                return
            if rem > SPIN_NS:
                due = ctypes.c_longlong(-((rem - SPIN_NS) // 100))
                k32.SetWaitableTimer(handle, ctypes.byref(due), 0, None, None, False)
                k32.WaitForSingleObject(handle, 0xFFFFFFFF)
            else:
                while time.perf_counter_ns() < target_ns:
                    pass
                return

    return wait_until, lambda: k32.CloseHandle(handle)


def make_sleep_timer():
    def wait_until(target_ns):
        rem = target_ns - time.perf_counter_ns()
        if rem > 0:
            time.sleep(rem / 1e9)
    return wait_until, lambda: None


def make_spin_timer():
    def wait_until(target_ns):
        while time.perf_counter_ns() < target_ns:
            pass
    return wait_until, lambda: None


def measure(rate_hz, duration_s, timer_name):
    period_ns = 1_000_000_000 // rate_hz
    makers = {"hires": make_hires_timer,
              "sleep": make_sleep_timer,
              "spin":  make_spin_timer}
    wait_fn, cleanup = makers[timer_name]()

    jitters = collections.deque(maxlen=200_000)
    overruns = 0
    OVERRUN_NS = period_ns * 2
    total = int(rate_hz * duration_s)

    next_tick = ((time.perf_counter_ns() // period_ns) + 1) * period_ns

    print(f"\nИзмерение: {rate_hz}Гц × {duration_s}с = {total} тиков "
          f"[{timer_name}]")
    print("Прогресс: ", end="", flush=True)

    try:
        for i in range(total):
            wait_fn(next_tick)
            fire = time.perf_counter_ns()
            err = abs(fire - next_tick)
            jitters.append(err)
            if err > OVERRUN_NS:
                overruns += 1
            next_tick += period_ns
            if i % (rate_hz * 2) == 0:
                print(".", end="", flush=True)
    finally:
        cleanup()

    print(" готово.")
    return list(jitters), overruns


def print_report(jitters, overruns, rate_hz, timer_name=""):
    if not jitters:
        print("Нет данных.")
        return

    sorted_j = sorted(jitters)
    n = len(sorted_j)
    avg_us = statistics.mean(jitters) / 1000
    std_us = statistics.stdev(jitters) / 1000
    p50_us = sorted_j[n // 2] / 1000
    p95_us = sorted_j[int(n * 0.95)] / 1000
    p99_us = sorted_j[int(n * 0.99)] / 1000
    max_us = sorted_j[-1] / 1000

    title = f"Jitter [{timer_name}]" if timer_name else "Jitter"
    print(f"\n{'═'*52}")
    print(f"  {title} ({n} измерений)")
    print(f"{'═'*52}")
    print(f"  avg    : {avg_us:8.1f} µs")
    print(f"  std    : {std_us:8.1f} µs")
    print(f"  p50    : {p50_us:8.1f} µs")
    print(f"  p95    : {p95_us:8.1f} µs")
    print(f"  p99    : {p99_us:8.1f} µs")
    print(f"  max    : {max_us:8.1f} µs")
    print(f"  overrun: {overruns:8d}")
    print(f"{'═'*52}")

    if avg_us < 150:
        verdict = "✅ ОТЛИЧНО  — система готова к 2кГц"
    elif avg_us < 350:
        verdict = "⚠️  НОРМА    — допустимо"
    elif avg_us < 800:
        verdict = "❌ ПЛОХО    — проверь приоритет процесса"
    else:
        verdict = "🔴 КРИТИЧНО — 2кГц недостижимо без RT-ядра"
    print(f"\n  {verdict}")

    # ASCII-гистограмма
    print("\n  Гистограмма (µs):")
    buckets = [0, 50, 100, 200, 300, 500, 1000, 2000, float("inf")]
    labels  = [" <50", "50-100", "100-200", "200-300",
               "300-500", "0.5-1ms", "1-2ms", " >2ms"]
    counts = [0] * (len(buckets) - 1)
    for j in jitters:
        us = j / 1000
        for k in range(len(buckets) - 1):
            if us < buckets[k + 1]:
                counts[k] += 1
                break
    max_c = max(counts) if counts else 1
    W = 28
    for label, count in zip(labels, counts):
        bar = "█" * int(count / max_c * W)
        pct = count / n * 100
        print(f"  {label:>8}µs |{bar:<{W}}| {count:6d} ({pct:5.1f}%)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--hz",      type=int,   default=2000)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--timer",   choices=["hires", "sleep", "spin"],
                        default="hires")
    parser.add_argument("--no-rt",   action="store_true")
    parser.add_argument("--compare", action="store_true",
                        help="Сравнить все три режима таймера")
    args = parser.parse_args()

    if not args.no_rt:
        setup_windows_rt()

    if args.compare:
        results = {}
        for t in ["sleep", "hires", "spin"]:
            j, o = measure(args.hz, 5.0, t)
            results[t] = (j, o)

        print("\n\n  СРАВНЕНИЕ ТАЙМЕРОВ:")
        print(f"  {'Таймер':<8} {'avg µs':>8} {'p99 µs':>8} {'overrun':>8}")
        print("  " + "─" * 38)
        for name, (j, o) in results.items():
            avg = statistics.mean(j) / 1000
            p99 = sorted(j)[int(len(j) * 0.99)] / 1000
            print(f"  {name:<8} {avg:>8.1f} {p99:>8.1f} {o:>8d}")
    else:
        jitters, overruns = measure(args.hz, args.seconds, args.timer)
        print_report(jitters, overruns, args.hz, args.timer)
