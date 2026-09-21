# Runs one full cycle of the paper-trading worker (collect, research, detect, evaluate, predict, paper-trade).
# Scheduled by the "CryptoAI-Tick" task. Output goes to worker\logs\tick.log with connection strings masked.
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force -Path logs | Out-Null
$log = Join-Path $PSScriptRoot "logs\tick.log"
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 2MB)) { Move-Item -Force $log "$log.old" }

$out = & python -W ignore -m crypto_ai.cli tick 2>&1 | Out-String
$out = $out -replace 'postgres(ql)?://\S+', '<hidden>'
$out = $out -replace '(?i)(api_key|apikey)=[^&\s]+', '$1=<hidden>'
# keep the log readable: the per-coin market snapshot dict is long
$out = $out -replace 'collect: \{.*?\} \| research', 'collect: ok | research'
Add-Content -Path $log -Value $out.TrimEnd()
