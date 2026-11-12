@echo off
setlocal
set "BASE=%~dp0"
echo Stopping workmom python processes under %BASE%
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*%BASE%*' -and $_.Name -like '*python*' } | ForEach-Object { Write-Host ('kill ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }"
echo done.
pause
