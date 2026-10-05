@echo off
setlocal
title SiteLink LAN access setup

rem Create a private-network-only inbound rule for the local demo server.
net session >nul 2>&1
if errorlevel 1 (
  echo Administrator permission is needed to add the Windows Firewall rule.
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; try { $name='SiteLink v4 LAN demo TCP 8000'; $rule=Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue; if ($rule) { $rule | Set-NetFirewallRule -Enabled True -Direction Inbound -Action Allow -Profile Private; $rule | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -RemoteAddress LocalSubnet; Write-Host 'Updated the existing SiteLink private-LAN firewall rule.' } else { New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 -RemoteAddress LocalSubnet -Profile Private | Out-Null; Write-Host 'Added a SiteLink firewall rule for TCP 8000 on private networks only.' } } catch { Write-Error $_; exit 1 }"
if errorlevel 1 (
  echo Could not configure Windows Firewall. Ask your administrator to allow inbound TCP 8000 on the private LAN.
  pause
  exit /b 1
)

echo.
echo Done. Connect only while this PC and teammates are on the same trusted Wi-Fi or LAN.
echo The rule is limited to the local subnet and the Windows Private profile.
pause
