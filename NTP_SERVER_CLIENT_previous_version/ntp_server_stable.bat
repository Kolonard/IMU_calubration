@echo off
title Windows NTP Server Setup
color 0A

echo WINDOWS 10/11 NTP SERVER SETUP
echo.

:: ADMIN CHECK
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] RUN THIS FILE AS ADMINISTRATOR
    pause
    exit /b
)

echo [1/8] Enabling HPET and stable timers...

bcdedit /set useplatformclock yes >nul
bcdedit /set disabledynamictick yes >nul
bcdedit /set useplatformtick yes >nul

echo Done.
echo.

echo [2/8] Configuring Windows Time service...

sc config w32time start= auto >nul

echo Done.
echo.

echo [3/8] Configuring NTP server registry...

reg add "HKLM\SYSTEM\CurrentControlSet\Services\W32Time\Parameters" /v Type /t REG_SZ /d NTP /f >nul

reg add "HKLM\SYSTEM\CurrentControlSet\Services\W32Time\Config" /v AnnounceFlags /t REG_DWORD /d 5 /f >nul

reg add "HKLM\SYSTEM\CurrentControlSet\Services\W32Time\Config" /v MaxPosPhaseCorrection /t REG_DWORD /d 4294967295 /f >nul

reg add "HKLM\SYSTEM\CurrentControlSet\Services\W32Time\Config" /v MaxNegPhaseCorrection /t REG_DWORD /d 4294967295 /f >nul

reg add "HKLM\SYSTEM\CurrentControlSet\Services\W32Time\Config" /v MinPollInterval /t REG_DWORD /d 4 /f >nul

reg add "HKLM\SYSTEM\CurrentControlSet\Services\W32Time\Config" /v MaxPollInterval /t REG_DWORD /d 6 /f >nul

reg add "HKLM\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\NtpServer" /v Enabled /t REG_DWORD /d 1 /f >nul

reg add "HKLM\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\NtpServer" /v InputProvider /t REG_DWORD /d 1 /f >nul

echo Done.
echo.

echo [4/8] Configuring reliable source...

w32tm /config /reliable:yes /update >nul

echo Done.
echo.

echo [5/8] Opening firewall UDP 123...

netsh advfirewall firewall add rule name="NTP UDP 123" dir=in action=allow protocol=UDP localport=123 >nul 2>&1

echo Done.
echo.

echo [6/8] Restarting Windows Time service...

net stop w32time >nul 2>&1
net start w32time >nul

echo Done.
echo.

echo [7/8] Applying configuration...

w32tm /config /update >nul
w32tm /resync /rediscover >nul 2>&1

echo Done.
echo.

echo [8/8] Current status:
echo.

w32tm /query /status

echo.
echo ==========================================
echo NTP SERVER CONFIGURED SUCCESSFULLY
echo ==========================================
echo.
echo IMPORTANT:
echo Reboot the PC once after setup.
echo.
pause