"""
read_imu_log.py
===============
Читатель бинарных логов формата IMULOG01.

Использование:
    from read_imu_log import read_log

    df = read_log("data_tcp.bin")
    print(df.head())
    print(df[["t_s", "gx_dps", "gy_dps", "gz_dps"]].describe())

    # Построить график
    import matplotlib.pyplot as plt
    plt.plot(df["t_s"], df["gz_dps"])
    plt.xlabel("Время, с"); plt.ylabel("Gz, °/с")
    plt.show()

Зависимости:
    pip install numpy pandas
    pip install matplotlib   # опционально, для графиков
"""

import struct
import sys
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone

# ─── Формат файла ────────────────────────────────────────────────

# Заголовок файла (64 байта)
FILE_HEADER_FMT  = struct.Struct('<8sHHHIqQ6x')
FILE_HEADER_SIZE = 64

# Запись (58 байт) — numpy dtype, все поля по имени
RECORD_DTYPE = np.dtype([
    ('marker',     'S4'),     # "DREC"  4B
    ('recv_ts_ns', '<u8'),    # время приёма  8B
    ('sync_seq',   '<u4'),    # номер sync-метки  4B
    ('sync_ts_ns', '<i8'),    # время sync-метки  8B
    ('delta_us',   '<i2'),    # отклонение, мкс  2B
    # raw IMU пакет (32 байта) разобран по полям:
    ('imu_hdr',    '<u2'),    # 0xC0C0  2B
    ('gx',         '<i4'),    # гироскоп  4B × 3
    ('gy',         '<i4'),
    ('gz',         '<i4'),
    ('ax',         '<i4'),    # акселерометр  4B × 3
    ('ay',         '<i4'),
    ('az',         '<i4'),
    ('tc',         '<i2'),    # температура  2B
    ('counter',    'u1'),     # счётчик пакетов  1B
    ('status',     'u1'),     # статус  1B
    ('crc',        '<u2'),    # CRC16  2B
])
# Итого: 4+8+4+8+2+2+4+4+4+4+4+4+2+1+1+2 = 58 байт ✓

# Коэффициенты пересчёта
GMULT = 1.085069e-6   # LSB → рад/с
AMULT = 5e-5          # LSB → м/с²
TMULT = 0.01          # LSB → °C


def read_log(filepath: str | Path) -> pd.DataFrame:
    """
    Читает .bin файл формата IMULOG01.
    Возвращает pandas DataFrame с физическими единицами.

    Колонки:
        t_s        float64   время с момента начала записи, секунды
        t_abs      float64   Unix-время, секунды (UTC)
        recv_ts_ns uint64    время приёма, нс
        sync_seq   uint32    номер sync-импульса
        delta_us   int16     отклонение от sync-импульса, мкс
        synced     bool      True если пакет привязан к sync-метке
        gx_dps     float64   гироскоп X, рад/с
        gy_dps     float64   гироскоп Y, рад/с
        gz_dps     float64   гироскоп Z, рад/с
        ax_ms2     float64   ускорение X, м/с²
        ay_ms2     float64   ускорение Y, м/с²
        az_ms2     float64   ускорение Z, м/с²
        tc_c       float64   температура, °C
        counter    uint8     счётчик пакетов (0–255)
        status     uint8     статус стенда
        crc_ok     bool      True если CRC верен
        loss       int       пропущенных пакетов перед этим
    """
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {path}")

    with open(path, "rb") as f:
        # ── Заголовок файла ──────────────────────────────────────
        hdr_raw = f.read(FILE_HEADER_SIZE)
        if len(hdr_raw) < FILE_HEADER_FMT.size:
            raise ValueError("Файл слишком короткий — нет заголовка")

        magic, version, frame_sz, record_sz, sample_hz, wall_epoch_ns, perf_start_ns = \
            FILE_HEADER_FMT.unpack(hdr_raw[:FILE_HEADER_FMT.size])

        if magic != b"IMULOG01":
            raise ValueError(f"Неверный формат: magic={magic!r}, ожидается b'IMULOG01'")

        # ── Данные (numpy, одно обращение к диску) ───────────────
        raw = np.frombuffer(f.read(), dtype=RECORD_DTYPE)

    if len(raw) == 0:
        raise ValueError("Файл не содержит записей")

    # ── Фильтр: только валидные записи ───────────────────────────
    valid_mask = raw["marker"] == b"DREC"
    if not valid_mask.all():
        n_bad = (~valid_mask).sum()
        print(f"[WARN] Пропущено {n_bad} записей с неверным маркером", file=sys.stderr)
    raw = raw[valid_mask]

    # ── CRC проверка ──────────────────────────────────────────────
    # Для проверки без внешней зависимости используем vectorized CRC
    # (быстрая версия через numpy для больших массивов)
    crc_ok = _check_crc_vectorized(raw)

    # ── Потери пакетов (поле counter, uint8, 0–255) ───────────────
    counters = raw["counter"].astype(np.int32)
    loss = np.zeros(len(raw), dtype=np.int32)
    diff = np.diff(counters)
    # Учитываем wraparound (255 → 0)
    loss[1:] = np.where(diff >= 0, diff - 1, diff + 256 - 1)
    loss[0]  = 0

    # ── Временна́я шкала ──────────────────────────────────────────
    recv_ts = raw["recv_ts_ns"].astype(np.float64)
    t_start = recv_ts[0]
    t_s     = (recv_ts - t_start) / 1e9          # секунды от старта
    t_abs   = recv_ts / 1e9                       # Unix-время (UTC)

    # Привязка к реальному времени через заголовок файла
    # wall_epoch_ns = time.time_ns() при старте записи
    t_abs_calibrated = (recv_ts - recv_ts[0] + wall_epoch_ns) / 1e9

    # ── Sync ─────────────────────────────────────────────────────
    UNSYNC_DELTA = -32768
    synced = raw["delta_us"] != UNSYNC_DELTA

    # ── Физические единицы ───────────────────────────────────────
    df = pd.DataFrame({
        "t_s"      : t_s,
        "t_abs"    : t_abs_calibrated,
        "recv_ts_ns": raw["recv_ts_ns"],
        "sync_seq" : raw["sync_seq"],
        "delta_us" : raw["delta_us"],
        "synced"   : synced,
        "gx_dps"   : raw["gx"].astype(np.float64) * GMULT,
        "gy_dps"   : raw["gy"].astype(np.float64) * GMULT,
        "gz_dps"   : raw["gz"].astype(np.float64) * GMULT,
        "ax_ms2"   : raw["ax"].astype(np.float64) * AMULT,
        "ay_ms2"   : raw["ay"].astype(np.float64) * AMULT,
        "az_ms2"   : raw["az"].astype(np.float64) * AMULT,
        "tc_c"     : raw["tc"].astype(np.float64) * TMULT,
        "counter"  : raw["counter"],
        "status"   : raw["status"],
        "crc_ok"   : crc_ok,
        "loss"     : loss,
    })

    return df


def print_info(filepath: str | Path):
    """Быстрый вывод информации о файле без загрузки данных в память."""
    path = Path(filepath)
    file_size = path.stat().st_size

    with open(path, "rb") as f:
        hdr_raw = f.read(FILE_HEADER_SIZE)

    magic, version, frame_sz, record_sz, sample_hz, wall_epoch_ns, perf_start_ns = \
        FILE_HEADER_FMT.unpack(hdr_raw[:FILE_HEADER_FMT.size])

    n_records = (file_size - FILE_HEADER_SIZE) // record_sz
    duration_s = n_records / sample_hz if sample_hz > 0 else 0

    rec_dt = datetime.fromtimestamp(wall_epoch_ns / 1e9, tz=timezone.utc)

    print(f"Файл:           {path.name}  ({file_size / 1024:.1f} КБ)")
    print(f"Формат:         {magic.decode()} v{version}")
    print(f"Записей:        {n_records:,}")
    print(f"Размер записи:  {record_sz} байт")
    print(f"Частота:        {sample_hz} Гц")
    print(f"Длительность:   {duration_s:.1f} с  ({duration_s/60:.2f} мин)")
    print(f"Записано:       {rec_dt:%Y-%m-%d %H:%M:%S} UTC")


# ─── Vectorized CRC16-CCITT ──────────────────────────────────────

def _crc16_ccitt_scalar(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xFFFF
    return crc


def _check_crc_vectorized(raw: np.ndarray) -> np.ndarray:
    """
    Проверяет CRC для каждой записи.
    Для больших файлов использует быструю векторную проверку через numpy.
    """
    n = len(raw)
    crc_ok = np.zeros(n, dtype=bool)

    # CRC считается над raw[2:30] (пропуская header C0C0 и сам CRC)
    # Поля: gx gy gz ax ay az tc counter status = 24+2+1+1 = 28 байт
    fields = ["gx", "gy", "gz", "ax", "ay", "az", "tc", "counter", "status"]

    for i in range(n):
        # Пересобираем данные для CRC из отдельных полей
        data_bytes = struct.pack(
            "<6iih2B",
            int(raw["gx"][i]), int(raw["gy"][i]), int(raw["gz"][i]),
            int(raw["ax"][i]), int(raw["ay"][i]), int(raw["az"][i]),
            int(raw["tc"][i]),
            int(raw["counter"][i]), int(raw["status"][i]),
        )
        expected_crc = _crc16_ccitt_scalar(data_bytes)
        crc_ok[i] = (int(raw["crc"][i]) == expected_crc)

    return crc_ok


# ─── CLI ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Чтение IMU лог-файла")
    p.add_argument("file",           help="Путь к .bin файлу")
    p.add_argument("--info",         action="store_true",
                   help="Только заголовок, без загрузки данных")
    p.add_argument("--head",         type=int, default=10,
                   help="Вывести первые N строк (default: 10)")
    p.add_argument("--csv",          metavar="OUT",
                   help="Сохранить в CSV файл")
    p.add_argument("--plot",         action="store_true",
                   help="Построить график (требует matplotlib)")
    args = p.parse_args()

    if args.info:
        print_info(args.file)
        sys.exit(0)

    print(f"Загружаем {args.file} ...")
    df = read_log(args.file)

    # ── Сводка ───────────────────────────────────────────────────
    sync_pct    = df["synced"].mean() * 100
    crc_err_pct = (~df["crc_ok"]).mean() * 100
    loss_total  = df["loss"].sum()
    duration    = df["t_s"].iloc[-1]

    print(f"\n{'='*52}")
    print(f"  Записей:      {len(df):,}")
    print(f"  Длительность: {duration:.1f} с")
    print(f"  Rate avg:     {len(df)/duration:.0f} pkt/s")
    print(f"  Sync:         {sync_pct:.1f}%")
    print(f"  CRC ошибок:   {crc_err_pct:.2f}%")
    print(f"  Потери пакетов: {loss_total}")
    print(f"  T среднее:    {df['tc_c'].mean():.2f} °C")
    print(f"{'='*52}\n")

    print("Первые строки:")
    cols = ["t_s", "synced", "delta_us", "gx_dps", "gy_dps", "gz_dps",
            "ax_ms2", "ay_ms2", "az_ms2", "tc_c", "counter", "crc_ok"]
    pd.set_option("display.float_format", "{:.6f}".format)
    pd.set_option("display.max_columns", 20)
    pd.set_option("display.width", 160)
    print(df[cols].head(args.head).to_string(index=True))

    # ── CSV ──────────────────────────────────────────────────────
    if args.csv:
        df.to_csv(args.csv, index=False)
        print(f"\nСохранено: {args.csv}  ({Path(args.csv).stat().st_size/1024:.0f} КБ)")

    # ── График ───────────────────────────────────────────────────
    if args.plot:
        try:
            import matplotlib.pyplot as plt

            fig, axes = plt.subplots(3, 1, figsize=(14, 8), sharex=True)
            fig.suptitle(f"IMU лог: {Path(args.file).name}", fontsize=12)

            axes[0].plot(df["t_s"], df["gz_dps"], "b-", lw=0.5, label="Gz")
            axes[0].plot(df["t_s"], df["gx_dps"], "r-", lw=0.5, alpha=0.7, label="Gx")
            axes[0].plot(df["t_s"], df["gy_dps"], "g-", lw=0.5, alpha=0.7, label="Gy")
            axes[0].set_ylabel("рад/с")
            axes[0].legend(loc="upper right", fontsize=8)
            axes[0].set_title("Гироскоп")

            axes[1].plot(df["t_s"], df["az_ms2"], "b-", lw=0.5, label="Az")
            axes[1].plot(df["t_s"], df["ax_ms2"], "r-", lw=0.5, alpha=0.7, label="Ax")
            axes[1].plot(df["t_s"], df["ay_ms2"], "g-", lw=0.5, alpha=0.7, label="Ay")
            axes[1].set_ylabel("м/с²")
            axes[1].legend(loc="upper right", fontsize=8)
            axes[1].set_title("Акселерометр")

            # Sync-метки как маркеры на оси
            synced_t = df.loc[df["synced"], "t_s"]
            if len(synced_t) > 0:
                axes[2].vlines(synced_t, 0, 1, colors="green", lw=0.3,
                               alpha=0.5, label=f"synced {sync_pct:.0f}%")
            unsynced_t = df.loc[~df["synced"], "t_s"]
            if len(unsynced_t) > 0:
                axes[2].vlines(unsynced_t, 0, 1, colors="red", lw=0.3,
                               alpha=0.3, label="unsynced")
            axes[2].set_ylabel("sync")
            axes[2].set_xlabel("Время, с")
            axes[2].legend(loc="upper right", fontsize=8)
            axes[2].set_ylim(0, 1.2)
            axes[2].set_title("Синхронизация")

            plt.tight_layout()
            plt.savefig(args.file.replace(".bin", "_plot.png"), dpi=150)
            print(f"\nГрафик сохранён: {args.file.replace('.bin', '_plot.png')}")
            plt.show()
        except ImportError:
            print("[WARN] matplotlib не установлен: pip install matplotlib")
