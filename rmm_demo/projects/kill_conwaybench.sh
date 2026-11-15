#!/usr/bin/env bash
# Stops the conwaybench console and its 86Box (Linux: pkill; Win10 MSYS has no pkill/pgrep: PowerShell).
# Always exits 0.
if command -v pkill >/dev/null 2>&1; then
  pkill -f "[r]mm-demo-55910" 2>/dev/null; sleep 2
  pkill -9 -f -- "-[P] .*rmm_groups/conwaybench/" 2>/dev/null
else
  powershell.exe -NoProfile -Command '$me=$PID; Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $me -and $_.CommandLine -match "rmm-demo-55910|rmm_groups.conwaybench" } | ForEach-Object { Write-Output ("kill " + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }' 2>&1 | tr -d '\r'
  sleep 2
fi
exit 0
