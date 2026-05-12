# Инструкция по запуску и тестированию

## Структура файлов проекта

```
project/
├── sync_marks.py           # общий SYNC_STORE (shared между модулями)
├── sync_generator.py       # генератор 2кГц UDP-импульсов
├── imu_logger.py           # асинхронный логгер RS-485 → .bin
│
├── tcp_adapter.py          # ЗАМЕНА main.py при тестировании с XP VM
├── bench_emulator.py       # эмулятор стенда (XP VM, Python 3.4+)
├── run_loopback_test.py    # полный тест без VM (всё на Windows 11)
│
├── measure_jitter.py       # измерение jitter таймера
└── test_timing.py          # pytest-тесты протокола и PLL
```

---

## Вариант 1: Быстрый тест без VM (только Windows 11)

### Установка зависимостей
```cmd
pip install pyserial serial-asyncio ntplib
```

### Запуск
```cmd
python run_loopback_test.py
```

С реальными данными из data.bin:
```cmd
python run_loopback_test.py --file data.bin --duration 30
```

### Ожидаемый вывод
```
══════════════════════════════════════════
  run_loopback_test.py  —  end-to-end loopback
  Python 3.11.x
  Длительность: 10 сек @ 2000 Гц
══════════════════════════════════════════
09:00:01.000  Синтетических кадров: 8000
09:00:01.001  Ожидание bench-сервера...
09:00:01.002  TCP: подключение к 127.0.0.1:55299 ...
09:00:01.003  TCP: подключено. Тест 10 сек.
09:00:06.000  Rate: 1998 pkt/s | Total: 9991 | Loss: 0 | CRC err: 0 | Sync: 99.8%
09:00:11.003  Валидация data_loopback.bin ...

══════════════════════════════════════════
  Результаты loopback-теста
══════════════════════════════════════════
  Заголовок файла :  ✅ OK
  Записей в файле :  19998  (ожидалось ≈ 20000)
  Захват данных   :  99.9%  (19998/20000)
  CRC OK          : 100.0%  (19998/19998)
  Синхронизовано  :  99.7%  (19941/19998)
  Потери пакетов  : 0
  Прыжки времени  : 0
══════════════════════════════════════════
  ✅ ТЕСТ ПРОЙДЕН — система работает корректно
```

---

## Вариант 2: Полный тест с XP VM

### Настройка сети виртуальной машины

**VirtualBox:**
1. VM → Settings → Network → Adapter 1
2. Attached to: **Host-only Adapter**
3. Name: VirtualBox Host-Only Network
4. Узнать IP XP VM: на XP запустить `ipconfig` → запомнить адрес (обычно `192.168.56.xxx`)

**VMware:**
1. VM → Settings → Network Adapter
2. Connection: **Host-only**
3. Узнать IP: `ipconfig` на XP → адрес `192.168.xxx.xxx`

> Проверить связь: `ping <IP_XP_VM>` с Windows 11

### Установка на XP VM (Python 3.4.3)

Python 3.4 не требует дополнительных библиотек для bench_emulator.py —
используются только стандартные модули (`socket`, `struct`, `threading`, `ctypes`).

```cmd
# Скопировать bench_emulator.py на XP VM (через общую папку или сеть)
# Запустить:
python bench_emulator.py --file data.bin --loop
```

Если data.bin нет на XP VM — сгенерирует синтетику автоматически:
```cmd
python bench_emulator.py --fake 10000 --loop
```

### Запуск на Windows 11

Открыть **два** терминала (запускать от Администратора для RT-приоритета):

**Терминал 1 — адаптер + логгер:**
```cmd
python tcp_adapter.py --bench-host 192.168.56.101 --retry
```

**Проверка записанного файла:**
```cmd
python imu_logger.py --read data_tcp.bin 50
```

### Порядок запуска

```
XP VM:        python bench_emulator.py --file data.bin --loop
Windows 11:   python tcp_adapter.py --bench-host 192.168.56.101 --retry
```

bench_emulator ждёт подключения, tcp_adapter подключается автоматически.
При `--retry` tcp_adapter переподключится если bench перезапустить.

---

## Вариант 3: Полный боевой запуск (RS-485 + реальный стенд)

```cmd
# Запуск от Администратора (для RT-приоритета)
python main.py
```

main.py запускает sync_generator + imu_logger в одном процессе.
Данные читаются с COM3 @ 921600, пишутся в data_async.bin.

---

## Измерение jitter перед запуском

Перед боевым запуском проверить, что Windows 11 может держать 2кГц:

```cmd
# Запустить от Администратора!
python measure_jitter.py --hz 2000 --seconds 10 --timer hires

# Сравнить все три режима таймера:
python measure_jitter.py --compare
```

**Нормальные результаты:**
```
  avg    :    85.3 µs   ← хорошо
  p99    :   210.7 µs   ← приемлемо (< 500 мкс)
  overrun:        0     ← нет перегрузок
  ✅ ОТЛИЧНО — система готова к 2кГц
```

**Если avg > 500 мкс:**
- Запускать от Администратора
- Закрыть фоновые процессы (антивирус, браузер)
- Отключить Windows Update на время записи
- Проверить, что режим питания = "Высокая производительность"

---

## Чтение и проверка записанного файла

```cmd
# Первые 20 записей:
python imu_logger.py --read data_tcp.bin 20

# Первые 100 записей:
python imu_logger.py --read data_tcp.bin 100
```

**Пример вывода:**
```
№  recv_ms   sync_seq     Δ,µs       Gx       Gy       Gz ...  CRC
─────────────────────────────────────────────────────────────────
0    0.000          0      +12      100      200      300 ...  OK
1    0.501          1       -8      101      198      299 ...  OK
2    0.999          2      +31       99      201      301 ...  OK
```

- `Δ,µs` — отклонение пакета от sync-метки. Хорошо: |Δ| < 300 мкс
- `CRC` — всегда должно быть OK
- `sync_seq` — номер ближайшей sync-метки, растёт монотонно

---

## Запуск unit-тестов

```cmd
pip install pytest
pytest test_timing.py -v
```

**Ожидаемый результат:**
```
test_timing.py::TestSyncProtocol::test_roundtrip_zeros         PASSED
test_timing.py::TestSyncProtocol::test_roundtrip_max_values    PASSED
test_timing.py::TestPLL::test_pll_converges_no_loss            PASSED
test_timing.py::TestPLL::test_anti_windup                      PASSED
test_timing.py::TestUDPLoopback::test_loopback_delivery        PASSED
...
20 passed in 4.32s
```

---

## Типичные проблемы

| Симптом | Причина | Решение |
|---------|---------|---------|
| `ConnectionRefusedError` | bench_emulator не запущен | Сначала запустить bench_emulator, потом tcp_adapter |
| CRC err > 0% | Неверный `SYNC_BYTES` | Проверить что `b'\xC0\xC0'` в imu_logger.py |
| Sync: 0% | SYNC_STORE не заполняется | tcp_adapter запускает sync в том же процессе — ок |
| Rate: 0 pkt/s | TCP не подключён | Проверить IP, порт, firewall |
| overrun > 100 | ОС вытесняет поток | Запустить от Администратора, закрыть браузер |
| `ImportError: sync_marks` | Неверная рабочая директория | Запускать из папки проекта |
| Python 3.4: `SyntaxError` | f-string в bench_emulator | Проверить версию: `python --version` |

---

## Минимальный firewall для теста

На Windows 11 (если XP VM не достучится):
```cmd
# Открыть TCP 55200 входящий:
netsh advfirewall firewall add rule name="IMU_TCP" dir=in action=allow protocol=TCP localport=55200

# Открыть UDP 55100 входящий (для bench_emulator):
netsh advfirewall firewall add rule name="IMU_UDP" dir=in action=allow protocol=UDP localport=55100
```

На XP VM — отключить встроенный firewall или добавить исключения аналогично.
