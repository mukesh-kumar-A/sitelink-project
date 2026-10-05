@echo off
setlocal
cd /d "%~dp0"
echo SiteLink will print the Team URL for devices on the same Wi-Fi/LAN.
echo If teammates need access, first run ALLOW-TEAM-ACCESS.bat as Administrator
echo and make sure this Wi-Fi network is set to Private in Windows Settings.
echo.
where py >nul 2>nul
if %errorlevel%==0 (
  start "SiteLink Server" cmd /k py server.py
) else (
  start "SiteLink Server" cmd /k python server.py
)
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:8000"
