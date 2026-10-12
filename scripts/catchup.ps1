# Post any missed slots (e.g. after a reboot / when the PC was off).
$ErrorActionPreference = "Stop"
Set-Location "$PSScriptRoot\.."
.\.venv\Scripts\python.exe -m src.scheduler --catchup