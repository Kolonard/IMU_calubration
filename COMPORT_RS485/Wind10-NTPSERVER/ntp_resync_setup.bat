@echo off
:: =============================================================
:: ntp_resync_setup.bat
:: =============================================================
:: Создаёт задачу в Планировщике задач Windows:
:: каждые 30 минут делает w32tm /resync /rediscover
::
:: Запускать ОДИН РАЗ от Администратора на каждом ПК (клиент и сервер).
:: =============================================================

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] RUN THIS FILE AS ADMINISTRATOR
    pause
    exit /b
)

echo Создаём задачу автоматического NTP resync...

:: Удалить старую задачу если есть
schtasks /delete /tn "NTP_Resync" /f >nul 2>&1

:: Создать новую: каждые 30 минут, без входа в систему
schtasks /create ^
    /tn "NTP_Resync" ^
    /tr "w32tm /resync /rediscover" ^
    /sc minute ^
    /mo 30 ^
    /ru SYSTEM ^
    /rl HIGHEST ^
    /f

if %errorlevel% equ 0 (
    echo [OK] Задача NTP_Resync создана: каждые 30 минут.
) else (
    echo [ERR] Не удалось создать задачу.
)

echo.
echo Проверка задачи:
schtasks /query /tn "NTP_Resync"
echo.
pause
