# RUN THIS YOURSELF, once, in a normal PowerShell window (not through Claude). It registers one Scheduled Task that starts the paper
# scalper (run_scalp.ps1) every time you log on and restarts it if it stops. No admin rights needed; it runs as you.
#
# Before running it:  1) copy .env.example to .env (repo root) and fill DATABASE_URL   2) pip install -r requirements.txt
#                     3) python -m crypto_ai.cli scalp-train      (once: downloads history, trains the model)
# To stop it later:   Unregister-ScheduledTask -TaskName CryptoAI-Scalper -Confirm:$false
$script = Join-Path $PSScriptRoot "run_scalp.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName "CryptoAI-Scalper" -Action $action -Trigger $trigger -Settings $settings -Description "Crypto AI Lab: paper scalper, a pass every 20 s (paper trading only)" -Force
Start-ScheduledTask -TaskName "CryptoAI-Scalper"
Write-Host "Installed and started. Check it with:  Get-ScheduledTask CryptoAI-Scalper | Select TaskName, State"
Write-Host "Log: $(Join-Path $PSScriptRoot 'logs\scalp.log')"
