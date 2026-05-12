"""
main.py — запускает генератор, читатель и логгер вместе.
"""

import threading
import logging
import signal
import sys

from sync_generator import run as run_generator_async, NTPClock, generator_loop
from serial_reader import RS485Reader
from sync_integrator import BinaryLogger
import ntplib

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d  %(message)s",
    datefmt="%H:%M:%S"
)

NTP_SERVER = "pool.ntp.org"

def main():
    # ── 1. NTP ───────────────────────────────────────────────
    ntp = NTPClock(NTP_SERVER)
    try:
        ntp.sync()
    except Exception as e:
        logging.warning("NTP: %s", e)
    threading.Thread(target=ntp.resync_loop, daemon=True).start()

    # ── 2. Генератор синхроимпульсов ─────────────────────────
    gen = threading.Thread(
        target=generator_loop, args=(ntp,), daemon=True, name="SyncGen"
    )
    gen.start()

    # ── 3. Логгер ────────────────────────────────────────────
    logger = BinaryLogger("data_log.bin")
    logger.start()

    # ── 4. RS-485 ридер ──────────────────────────────────────
    reader = RS485Reader(on_packet=logger.on_packet)
    reader.start()

    # ── 5. Обработка Ctrl+C ──────────────────────────────────
    def shutdown(sig, frame):
        logging.info("Остановка...")
        reader.stop()
        logger.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    gen.join()


if __name__ == "__main__":
    main()