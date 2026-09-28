"""
test_timing.py
==============
Полный тест-сюит системы синхронизации.
Запуск: pytest test_timing.py -v

Покрывает:
  - корректность пакетного протокола
  - точность таймера (loopback)
  - PLL сходимость
  - статистику jitter
  - потери пакетов
"""

import time
import socket
import struct
import threading
import collections
import statistics
import pytest
import ctypes
import sys
from dataclasses import dataclass
from typing import List


# ══════════════════════════════════════════════════════════════════
# Копии тестируемых классов (импортируй свои, если уже есть модули)
# ══════════════════════════════════════════════════════════════════

MAGIC = b"SYNC"
VERSION = 1
PKT_FMT = "!4sB3xQQQ"
PKT_SIZE = struct.calcsize(PKT_FMT)


@dataclass
class SyncPacket:
    seq: int
    sender_monotonic_ns: int
    logical_tick_ns: int

    def pack(self) -> bytes:
        return struct.pack(
            PKT_FMT, MAGIC, VERSION,
            self.seq, self.sender_monotonic_ns, self.logical_tick_ns
        )

    @staticmethod
    def unpack(data: bytes):
        if len(data) < PKT_SIZE:
            raise ValueError("packet too small")
        magic, version, seq, mono_ns, logical_ns = struct.unpack(
            PKT_FMT, data[:PKT_SIZE]
        )
        if magic != MAGIC:
            raise ValueError("invalid magic")
        if version != VERSION:
            raise ValueError("unsupported version")
        return SyncPacket(seq=seq, sender_monotonic_ns=mono_ns,
                          logical_tick_ns=logical_ns)


class ClockRecoveryPLL:
    """Исправленный PLL с anti-windup и защитой от потерь."""

    MAX_INTEGRAL_NS = 1_000_000_000  # 1 с
    KP = 0.00002
    KI = 0.000001

    def __init__(self):
        self.initialized = False
        self.phase_error_integral = 0.0
        self.frequency_adjust = 1.0
        self.last_sender_tick = 0
        self.last_arrival_ns = 0
        self.recovered_sender_time_ns = 0
        self._phase_errors: collections.deque = collections.deque(maxlen=5000)

    def update(self, sender_tick_ns: int, arrival_ns: int):
        if not self.initialized:
            self.initialized = True
            self.last_sender_tick = sender_tick_ns
            self.last_arrival_ns = arrival_ns
            self.recovered_sender_time_ns = sender_tick_ns
            return None

        sender_delta = sender_tick_ns - self.last_sender_tick
        arrival_delta = arrival_ns - self.last_arrival_ns
        predicted_delta = sender_delta * self.frequency_adjust
        phase_error = arrival_delta - predicted_delta

        # Anti-windup clamp
        self.phase_error_integral = max(
            -self.MAX_INTEGRAL_NS,
            min(self.MAX_INTEGRAL_NS,
                self.phase_error_integral + phase_error)
        )

        correction = (self.KP * phase_error +
                      self.KI * self.phase_error_integral)
        self.frequency_adjust += correction * 1e-15

        self.last_sender_tick = sender_tick_ns
        self.last_arrival_ns = arrival_ns
        self.recovered_sender_time_ns += int(predicted_delta)

        self._phase_errors.append(phase_error)

        return {
            "phase_error_us": phase_error / 1000,
            "freq_adjust_ppm": (self.frequency_adjust - 1.0) * 1_000_000,
        }

    def handle_loss(self, missed_count: int):
        """Сброс интегратора при потере пакетов."""
        self.phase_error_integral = 0.0
        # Экстраполируем время
        self.recovered_sender_time_ns += missed_count * 500_000  # 500мкс/пакет

    @property
    def phase_rms_us(self) -> float:
        if not self._phase_errors:
            return 0.0
        return (sum(e**2 for e in self._phase_errors)
                / len(self._phase_errors)) ** 0.5 / 1000


# ══════════════════════════════════════════════════════════════════
# ТЕСТ 1: Протокол — pack/unpack roundtrip
# ══════════════════════════════════════════════════════════════════

class TestSyncProtocol:

    def test_packet_size(self):
        """Размер пакета фиксирован."""
        pkt = SyncPacket(seq=0, sender_monotonic_ns=0, logical_tick_ns=0)
        assert len(pkt.pack()) == PKT_SIZE

    def test_roundtrip_zeros(self):
        pkt = SyncPacket(seq=0, sender_monotonic_ns=0, logical_tick_ns=0)
        restored = SyncPacket.unpack(pkt.pack())
        assert restored.seq == 0
        assert restored.sender_monotonic_ns == 0
        assert restored.logical_tick_ns == 0

    def test_roundtrip_max_values(self):
        """uint64 максимум не вызывает переполнения."""
        pkt = SyncPacket(
            seq=2**64 - 1,
            sender_monotonic_ns=2**64 - 1,
            logical_tick_ns=2**64 - 1,
        )
        restored = SyncPacket.unpack(pkt.pack())
        assert restored.seq == 2**64 - 1

    def test_roundtrip_realistic(self):
        """Реалистичные значения: seq=100000, ts ~ 50 сек в нс."""
        pkt = SyncPacket(
            seq=100_000,
            sender_monotonic_ns=50_000_000_000,
            logical_tick_ns=50_000_000_000,
        )
        restored = SyncPacket.unpack(pkt.pack())
        assert restored.seq == 100_000
        assert restored.sender_monotonic_ns == 50_000_000_000

    def test_invalid_magic(self):
        bad = b"XXXX" + b"\x01\x00\x00\x00" + b"\x00" * 24
        with pytest.raises(ValueError, match="invalid magic"):
            SyncPacket.unpack(bad)

    def test_invalid_version(self):
        pkt = SyncPacket(seq=0, sender_monotonic_ns=0, logical_tick_ns=0)
        raw = bytearray(pkt.pack())
        raw[4] = 99  # version byte
        with pytest.raises(ValueError, match="unsupported version"):
            SyncPacket.unpack(bytes(raw))

    def test_too_short(self):
        with pytest.raises(ValueError, match="too small"):
            SyncPacket.unpack(b"\x00" * 4)

    def test_seq_monotonic_across_packets(self):
        """Последовательные пакеты имеют возрастающий seq."""
        packets = [
            SyncPacket(seq=i, sender_monotonic_ns=i * 500_000,
                       logical_tick_ns=i * 500_000)
            for i in range(100)
        ]
        restored = [SyncPacket.unpack(p.pack()) for p in packets]
        seqs = [r.seq for r in restored]
        assert seqs == list(range(100))


# ══════════════════════════════════════════════════════════════════
# ТЕСТ 2: Таймер — точность через loopback UDP
# ══════════════════════════════════════════════════════════════════

class TestTimerAccuracy:
    """
    Тест без реального WindowsHighResTimer — использует time.sleep
    как baseline. Чтобы тестировать реальный таймер, замени
    _wait_until на timer.wait_until_ns.
    """

    RATE_HZ    = 2000
    PERIOD_NS  = 1_000_000_000 // RATE_HZ   # 500 000 нс
    N_PULSES   = 200
    # Допустимый jitter для теста (мягче чем в продакшне — CI-friendly)
    MAX_AVG_JITTER_US  = 2000   # 2 мс avg в тестовой среде
    MAX_P99_JITTER_US  = 5000   # 5 мс p99

    def _software_wait(self, target_ns: int):
        """Чистый busy-wait — baseline измерение."""
        while time.perf_counter_ns() < target_ns:
            pass

    def test_interval_distribution(self):
        """
        Генерируем N импульсов с заданным интервалом,
        проверяем распределение реальных интервалов.
        """
        intervals_ns: List[int] = []

        next_tick = time.perf_counter_ns() + self.PERIOD_NS
        prev_fire = None

        for _ in range(self.N_PULSES):
            self._software_wait(next_tick)
            fire = time.perf_counter_ns()

            if prev_fire is not None:
                intervals_ns.append(fire - prev_fire)

            prev_fire = fire
            next_tick += self.PERIOD_NS

        avg_ns   = statistics.mean(intervals_ns)
        stdev_ns = statistics.stdev(intervals_ns)
        p99_ns   = sorted(intervals_ns)[int(len(intervals_ns) * 0.99)]

        avg_jitter_us  = abs(avg_ns - self.PERIOD_NS) / 1000
        p99_jitter_us  = abs(p99_ns - self.PERIOD_NS) / 1000
        stdev_us       = stdev_ns / 1000

        print(f"\n  Timer accuracy ({self.N_PULSES} pulses @ {self.RATE_HZ}Hz):")
        print(f"  avg_interval={avg_ns/1e6:.3f}ms  "
              f"stdev={stdev_us:.1f}µs  "
              f"p99_jitter={p99_jitter_us:.1f}µs")

        assert avg_jitter_us < self.MAX_AVG_JITTER_US, (
            f"Средний jitter {avg_jitter_us:.1f}µs > {self.MAX_AVG_JITTER_US}µs"
        )
        assert p99_jitter_us < self.MAX_P99_JITTER_US, (
            f"P99 jitter {p99_jitter_us:.1f}µs > {self.MAX_P99_JITTER_US}µs"
        )

    def test_no_backward_time(self):
        """perf_counter_ns никогда не идёт назад."""
        prev = time.perf_counter_ns()
        for _ in range(10_000):
            now = time.perf_counter_ns()
            assert now >= prev, f"Время пошло назад: {now} < {prev}"
            prev = now

    def test_overrun_detection(self):
        """
        Симулируем задержку (sleep) и проверяем,
        что overrun detection срабатывает.
        """
        PERIOD_NS = self.PERIOD_NS
        OVERRUN_THRESHOLD_NS = 5_000_000  # 5мс

        overruns = 0
        next_tick = time.perf_counter_ns()

        for i in range(10):
            next_tick += PERIOD_NS
            now = time.perf_counter_ns()
            lateness = now - next_tick

            if i == 5:
                # Симулируем задержку планировщика
                time.sleep(0.020)  # 20мс
                now = time.perf_counter_ns()
                lateness = now - next_tick

            if lateness > OVERRUN_THRESHOLD_NS:
                overruns += 1
                next_tick = now + PERIOD_NS  # recovery

        assert overruns >= 1, "Overrun detection не сработал"


# ══════════════════════════════════════════════════════════════════
# ТЕСТ 3: PLL сходимость
# ══════════════════════════════════════════════════════════════════

class TestPLL:

    def _simulate_network(self,
                          n_packets: int,
                          base_delay_ns: int = 1_000_000,
                          jitter_ns: int = 200_000,
                          drift_ppm: float = 10.0,
                          loss_rate: float = 0.0):
        """
        Симулирует отправку/приём пакетов:
          base_delay: постоянная задержка сети
          jitter_ns:  случайный разброс задержки
          drift_ppm:  дрейф часов приёмника (реалистично: 10 ppm)
          loss_rate:  доля потерянных пакетов [0, 1)
        """
        import random
        rng = random.Random(42)

        PERIOD_NS = 500_000
        pll = ClockRecoveryPLL()

        sender_tick = 0
        receiver_local = 0
        receiver_drift_factor = 1.0 + drift_ppm * 1e-6

        phase_errors = []
        last_seq = -1

        for seq in range(n_packets):
            sender_tick += PERIOD_NS

            # Потеря пакета
            if rng.random() < loss_rate:
                continue

            # Разрыв в seq
            if last_seq >= 0 and seq != last_seq + 1:
                pll.handle_loss(seq - last_seq - 1)
            last_seq = seq

            jitter = int(rng.gauss(0, jitter_ns / 3))
            delay = base_delay_ns + jitter

            # Локальное время получателя с дрейфом
            receiver_local = int(sender_tick * receiver_drift_factor) + delay

            result = pll.update(sender_tick, receiver_local)
            if result:
                phase_errors.append(abs(result["phase_error_us"]))

        return phase_errors, pll

    def test_pll_converges_no_loss(self):
        """PLL должен сойтись: RMS фазовой ошибки < 500 мкс."""
        errors, pll = self._simulate_network(
            n_packets=4000,
            base_delay_ns=1_000_000,
            jitter_ns=100_000,
            drift_ppm=10.0,
            loss_rate=0.0,
        )
        # Берём последние 1000 пакетов (после разогрева)
        tail = errors[-1000:]
        rms = (sum(e**2 for e in tail) / len(tail)) ** 0.5
        print(f"\n  PLL RMS (tail 1000): {rms:.1f} µs")
        assert rms < 500, f"PLL не сошёлся: RMS={rms:.1f}µs"

    def test_pll_handles_packet_loss(self):
        """PLL остаётся стабильным при 5% потерях."""
        errors, pll = self._simulate_network(
            n_packets=5000,
            base_delay_ns=1_000_000,
            jitter_ns=150_000,
            drift_ppm=10.0,
            loss_rate=0.05,
        )
        assert len(errors) > 100, "Слишком мало данных"
        tail = errors[-500:]
        rms = (sum(e**2 for e in tail) / len(tail)) ** 0.5
        print(f"\n  PLL RMS with 5% loss: {rms:.1f} µs")
        assert rms < 1000, f"PLL нестабилен при потерях: RMS={rms:.1f}µs"

    def test_anti_windup(self):
        """Интегральный член не растёт бесконечно."""
        errors, pll = self._simulate_network(
            n_packets=10000,
            drift_ppm=100.0,  # экстремальный дрейф
        )
        assert abs(pll.phase_error_integral) <= pll.MAX_INTEGRAL_NS * 1.01, (
            f"Anti-windup не работает: integral={pll.phase_error_integral:.0f}"
        )

    def test_frequency_adjust_bounded(self):
        """frequency_adjust не уходит в бесконечность."""
        _, pll = self._simulate_network(n_packets=10000, drift_ppm=50.0)
        assert 0.9 < pll.frequency_adjust < 1.1, (
            f"frequency_adjust вышел за границы: {pll.frequency_adjust}"
        )


# ══════════════════════════════════════════════════════════════════
# ТЕСТ 4: UDP Loopback — реальная отправка/приём пакетов
# ══════════════════════════════════════════════════════════════════

class TestUDPLoopback:
    """
    Отправляем реальные UDP-пакеты на localhost и проверяем:
      - все пакеты доставлены
      - задержка приемлемая
      - нет искажений данных
    """

    LOOPBACK_PORT = 55199
    N_PACKETS     = 100

    def test_loopback_delivery(self):
        """Все пакеты доходят до localhost без потерь."""
        received = []

        def receiver():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", self.LOOPBACK_PORT))
            sock.settimeout(2.0)
            try:
                for _ in range(self.N_PACKETS):
                    data, _ = sock.recvfrom(64)
                    arrival = time.perf_counter_ns()
                    pkt = SyncPacket.unpack(data)
                    received.append((pkt, arrival))
            except socket.timeout:
                pass
            finally:
                sock.close()

        recv_thread = threading.Thread(target=receiver, daemon=True)
        recv_thread.start()
        time.sleep(0.05)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("127.0.0.1", self.LOOPBACK_PORT))

        sent_times = []
        for i in range(self.N_PACKETS):
            ts = time.perf_counter_ns()
            pkt = SyncPacket(seq=i, sender_monotonic_ns=ts,
                             logical_tick_ns=i * 500_000)
            sock.send(pkt.pack())
            sent_times.append(ts)
            time.sleep(0.001)  # 1кГц для теста

        sock.close()
        recv_thread.join(timeout=3.0)

        assert len(received) == self.N_PACKETS, (
            f"Получено {len(received)}/{self.N_PACKETS} пакетов"
        )

    def test_loopback_latency(self):
        """RTT на localhost < 5 мс (обычно < 100 мкс)."""
        latencies_us = []

        recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        recv_sock.bind(("127.0.0.1", self.LOOPBACK_PORT + 1))
        recv_sock.settimeout(1.0)

        send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        send_sock.connect(("127.0.0.1", self.LOOPBACK_PORT + 1))

        for i in range(50):
            ts_send = time.perf_counter_ns()
            pkt = SyncPacket(seq=i, sender_monotonic_ns=ts_send,
                             logical_tick_ns=i * 500_000)
            send_sock.send(pkt.pack())

            try:
                data, _ = recv_sock.recvfrom(64)
                ts_recv = time.perf_counter_ns()
                restored = SyncPacket.unpack(data)
                latency_us = (ts_recv - restored.sender_monotonic_ns) / 1000
                latencies_us.append(latency_us)
            except socket.timeout:
                pass

        send_sock.close()
        recv_sock.close()

        assert latencies_us, "Нет данных о задержке"
        avg_us = statistics.mean(latencies_us)
        p99_us = sorted(latencies_us)[int(len(latencies_us) * 0.99)]

        print(f"\n  UDP loopback: avg={avg_us:.1f}µs  p99={p99_us:.1f}µs")
        assert p99_us < 5000, f"P99 latency слишком высокая: {p99_us:.1f}µs"

    def test_seq_integrity(self):
        """seq в принятых пакетах совпадает с отправленным."""
        received_seqs = []

        def receiver():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", self.LOOPBACK_PORT + 2))
            sock.settimeout(2.0)
            try:
                for _ in range(self.N_PACKETS):
                    data, _ = sock.recvfrom(64)
                    pkt = SyncPacket.unpack(data)
                    received_seqs.append(pkt.seq)
            except socket.timeout:
                pass
            finally:
                sock.close()

        recv_thread = threading.Thread(target=receiver, daemon=True)
        recv_thread.start()
        time.sleep(0.05)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("127.0.0.1", self.LOOPBACK_PORT + 2))
        for i in range(self.N_PACKETS):
            pkt = SyncPacket(seq=i, sender_monotonic_ns=i * 500_000,
                             logical_tick_ns=i * 500_000)
            sock.send(pkt.pack())
            time.sleep(0.001)
        sock.close()

        recv_thread.join(timeout=3.0)
        assert received_seqs == list(range(self.N_PACKETS)), (
            "Нарушение порядка или потеря пакетов на loopback"
        )


# ══════════════════════════════════════════════════════════════════
# ТЕСТ 5: Статистика jitter (deque vs list производительность)
# ══════════════════════════════════════════════════════════════════

class TestStatistics:

    def test_deque_is_fast(self):
        """deque(maxlen=5000).append() быстрее list.pop(0) в 100x."""
        import timeit

        n = 10_000

        def with_list():
            buf = []
            for i in range(n):
                buf.append(i)
                if len(buf) > 5000:
                    buf.pop(0)

        def with_deque():
            buf = collections.deque(maxlen=5000)
            for i in range(n):
                buf.append(i)

        t_list  = timeit.timeit(with_list,  number=10)
        t_deque = timeit.timeit(with_deque, number=10)

        speedup = t_list / t_deque
        print(f"\n  list: {t_list:.3f}s  deque: {t_deque:.3f}s  "
              f"speedup: {speedup:.1f}x")
        assert speedup > 5, f"deque должен быть быстрее list минимум в 5x"

    def test_jitter_percentiles(self):
        """Percentile-расчёт из реальных jitter данных."""
        # Симулируем нормальное распределение jitter ~200мкс std
        import random
        rng = random.Random(0)
        samples = [abs(int(rng.gauss(0, 200_000))) for _ in range(2000)]

        sorted_s = sorted(samples)
        p50 = sorted_s[len(sorted_s) // 2] / 1000
        p95 = sorted_s[int(len(sorted_s) * 0.95)] / 1000
        p99 = sorted_s[int(len(sorted_s) * 0.99)] / 1000
        avg = statistics.mean(samples) / 1000

        print(f"\n  Jitter: avg={avg:.1f}µs  p50={p50:.1f}µs  "
              f"p95={p95:.1f}µs  p99={p99:.1f}µs")

        # Для нормального распределения p99 ≈ 2.3 * std
        assert p99 < 700, f"p99={p99:.1f}µs выше ожидаемого"
