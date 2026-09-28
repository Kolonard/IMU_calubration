@echo off
title Windows 11 NTP Client Setup
color 0B

:: =============================================
:: ntp_client_win11.bat
:: Запускать от Администратора на ПК записи.
:: Настроить IP сервера ниже.
:: =============================================

set NTP_SERVER=192.168.137.1

echo WINDOWS 11 NTP CLIENT SETUP
echo.
echo NTP SERVER: %NTP_SERVER%
echo.

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] RUN THIS FILE AS ADMINISTRATOR
    pause
    exit /b
)

echo [1/7] Enabling HPET and stable timers...
bcdedit /set useplatformclock yes >nul
bcdedit /set disabledynamictick yes >nul
bcdedit /set useplatformtick yes >nul
echo Done.
echo.

echo [2/7] Configuring Windows Time service...
sc config w32time start= auto >nul
echo Done.
echo.

echo [3/7] Configuring NTP client registry...
reg add "HKLM\SYSTEM\CurrentControlSet\Services\W32Time\Parameters" /v Type /t REG_SZ /d NTP /f >nul
reg add "HKLM\SYSTEM\CurrentControlSet\Services\W32Time\Config" /v MaxPosPhaseCorrection /t REG_DWORD /d 4294967295 /f >nul
reg add "HKLM\SYSTEM\CurrentControlSet\Services\W32Time\Config" /v MaxNegPhaseCorrection /t REG_DWORD /d 4294967295 /f >nul
reg add "HKLM\SYSTEM\CurrentControlSet\Services\W32Time\Config" /v MinPollInterval /t REG_DWORD /d 4 /f >nul
reg add "HKLM\SYSTEM\CurrentControlSet\Services\W32Time\Config" /v MaxPollInterval /t REG_DWORD /d 6 /f >nul
reg add "HKLM\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\NtpClient" /v SpecialPollInterval /t REG_DWORD /d 16 /f >nul
echo Done.
echo.

echo [4/7] Setting NTP server...
w32tm /config /manualpeerlist:"%NTP_SERVER%,0x8" /syncfromflags:manual /update >nul
echo Done.
echo.

echo [5/7] Restarting Windows Time service...
net stop w32time >nul 2>&1
net start w32time >nul
echo Done.
echo.

echo [6/7] Waiting for service initialization...
timeout /t 5 >nul
echo Done.
echo.

echo [7/7] Forcing synchronization...
w32tm /resync /rediscover
echo.
echo Current source:
w32tm /query /source
echo.
echo Current status:
w32tm /query /status

echo.
echo ==========================================
echo NTP CLIENT (Win11) CONFIGURED SUCCESSFULLY
echo ==========================================
echo.
echo IMPORTANT: Reboot the PC once after setup.
echo.
pause
