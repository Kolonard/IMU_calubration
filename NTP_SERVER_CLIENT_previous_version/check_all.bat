@echo off
setlocal enabledelayedexpansion
title System Check - IMU Logger Pre-Flight
color 0E

:: =============================================================
:: check_all.bat  —  Win11 ПК записи
:: =============================================================
:: Полная проверка системы перед запуском imu_logger.py
:: Запускать от Администратора.
::
:: Использование:
::   check_all.bat 192.168.137.1
::   (первый аргумент — IP NTP-сервера Windows 10)
:: =============================================================

set NTP_SERVER=%1
if "%NTP_SERVER%"=="" set NTP_SERVER=192.168.137.1

echo =============================================================
echo   ПРЕДПОЛЕТНАЯ ПРОВЕРКА СИСТЕМЫ IMU-ЛОГГЕРА
echo   NTP-сервер: %NTP_SERVER%
echo =============================================================
echo.

:: ── Проверка прав администратора ─────────────────────────────
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ОШИБКА] Запусти от имени Администратора!
    pause
    exit /b 1
)

:: =============================================================
echo [1/6] Проверка сетевой связи с NTP-сервером...
echo -------------------------------------------------------------
ping -n 3 %NTP_SERVER% >nul 2>&1
if !errorlevel! equ 0 (
    echo   [OK] Сервер %NTP_SERVER% отвечает на ping
) else (
    echo   [ПРОБЛЕМА] Сервер %NTP_SERVER% недоступен!
    echo              Проверь Ethernet, IP, роутер.
)
echo.

:: =============================================================
echo [2/6] Проверка источника времени NTP...
echo -------------------------------------------------------------
for /f "tokens=*" %%s in ('w32tm /query /source 2^>nul') do set NTP_SRC=%%s
echo   Текущий источник: !NTP_SRC!
echo !NTP_SRC! | find "%NTP_SERVER%" >nul
if !errorlevel! equ 0 (
    echo   [OK] Синхронизация с нашим NTP-сервером
) else (
    echo !NTP_SRC! | find /i "Local CMOS" >nul
    if !errorlevel! equ 0 (
        echo   [ПРОБЛЕМА] Источник = локальные часы, НЕ наш сервер!
        echo              Перезапусти ntp_client_win11.bat
    ) else (
        echo   [ВНИМАНИЕ] Источник не распознан, проверь вручную
    )
)
echo.

:: =============================================================
echo [3/6] Статус службы времени...
echo -------------------------------------------------------------
w32tm /query /status | find "Last Successful Sync Time"
w32tm /query /status | find "Stratum"
w32tm /query /status | find "Source"
echo.

:: =============================================================
echo [4/6] Измерение задержки NTP (10 замеров)...
echo -------------------------------------------------------------
echo   offset = расхождение часов, delay = задержка сети
echo.
w32tm /stripchart /computer:%NTP_SERVER% /samples:10 /dataonly
echo.
echo   Норма: offset стабильный, в пределах +/- 5 мс
echo.

:: =============================================================
echo [5/6] Проверка jitter таймера (spin, 5 сек)...
echo -------------------------------------------------------------
where python >nul 2>&1
if !errorlevel! equ 0 (
    python measure_jitter.py --hz 2000 --seconds 5 --timer spin 2>nul
) else (
    echo   [ПРОПУСК] Python не найден в PATH
)
echo.

:: =============================================================
echo [6/6] Проверка наличия файлов проекта...
echo -------------------------------------------------------------
set MISSING=0
for %%f in (imu_logger.py sync_marks.py read_imu_log.py) do (
    if exist "%%f" (
        echo   [OK] %%f
    ) else (
        echo   [НЕТ] %%f  ^<-- файл отсутствует!
        set /a MISSING+=1
    )
)
echo.

:: ── Проверка COM-порта ────────────────────────────────────────
echo   Доступные COM-порты:
powershell -Command "Get-WmiObject Win32_SerialPort | Select-Object DeviceID, Description | Format-Table -AutoSize" 2>nul
if !errorlevel! neq 0 (
    echo   [ИНФО] Проверь COM-порт в Диспетчере устройств
)
echo.

:: =============================================================
echo =============================================================
echo   ИТОГ ПРОВЕРКИ
echo =============================================================
if !MISSING! equ 0 (
    echo   Файлы проекта: все на месте
) else (
    echo   Файлы проекта: отсутствует !MISSING! файл^(ов^)
)
echo.
echo   Перед запуском убедись:
echo     - offset NTP стабильный ^(шаг 4^)
echo     - jitter spin avg ^< 50 мкс ^(шаг 5^)
echo     - нужный COM-порт виден ^(шаг 6^)
echo.
echo   Если всё в норме — запускай:
echo     python imu_logger.py
echo.
echo   FTDI latency timer напоминание:
echo     Диспетчер устройств -^> COM-порт -^> Свойства -^>
echo     Параметры порта -^> Дополнительно -^> Latency Timer = 1 мс
echo =============================================================
echo.
pause
endlocal
