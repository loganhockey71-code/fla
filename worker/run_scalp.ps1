# Runs the paper scalper on THIS machine: a pass every 20 seconds, forever, plus the news polling, price snapshots, grading and
# learning it needs (python -m crypto_ai.cli scalp). If the Python process ever dies it is restarted after 15 s.
# Paper trading only. Output goes to worker\logs\scalp.log with connection strings and API keys masked.
# Launched by the Windows Scheduled Task that install_local_scalper.ps1 creates (you run that script yourself; it is not run for you).
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force -Path logs | Out-Null
$log = Join-Path $PSScriptRoot "logs\scalp.log"

while ($true) {
    if ((Test-Path $log) -and ((Get-Item $log).Length -gt 5MB)) { Move-Item -Force $log "$log.old" }
    "[$(Get-Date -Format o)] starting scalper" | Add-Content -Path $log
    & python -W ignore -m crypto_ai.cli scalp --every 20 2>&1 | ForEach-Object {
        ($_.ToString() -replace 'postgres(ql)?://\S+', '<hidden>') -replace '(?i)(api_key|apikey)=[^&\s]+', '$1=<hidden>'
    } | Add-Content -Path $log
    "[$(Get-Date -Format o)] scalper exited (code $LASTEXITCODE) - restarting in 15 s" | Add-Content -Path $log
    Start-Sleep -Seconds 15
}
