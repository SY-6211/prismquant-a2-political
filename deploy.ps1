param(
 [ValidateSet('Check','Start','Stop','Status','RegisterStartup','UnregisterStartup')][string]$Action='Check',
 [switch]$Collector
)
$ErrorActionPreference='Stop'
$deploymentPython=Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $deploymentPython)) {throw 'Create .venv and install the module dependencies first.'}
Push-Location $PSScriptRoot
try {
 switch ($Action) {
  'Check' { & $deploymentPython -X utf8 -m app.delivery_check; if ($LASTEXITCODE -ne 0){throw 'Deployment check failed.'} }
  'Start' {
   & $deploymentPython -X utf8 -m app.stack start
   if ($LASTEXITCODE -ne 0){throw 'API supervisor did not start; inspect port ownership and configuration.'}
   if ($Collector) {& $deploymentPython -X utf8 -m app.service_guard start; if ($LASTEXITCODE -ne 0){throw 'Collector guardian failed.'}}
  }
  'Stop' {
   if ($Collector) {& $deploymentPython -X utf8 -m app.service_guard stop; if ($LASTEXITCODE -ne 0){throw 'Collector stop failed.'}}
   & $deploymentPython -X utf8 -m app.stack stop
   if ($LASTEXITCODE -ne 0){throw 'API supervisor stop failed.'}
  }
  'Status' { & $deploymentPython -X utf8 -m app.stack status; if ($LASTEXITCODE -ne 0){throw 'Status unavailable.'} }
  'RegisterStartup' {
   $deploymentUser=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
   $deploymentHash=[System.BitConverter]::ToString([System.Security.Cryptography.SHA256]::Create().ComputeHash([System.Text.Encoding]::UTF8.GetBytes($PSScriptRoot))).Replace('-','').Substring(0,12)
   $deploymentTask='PrismQuant-'+$deploymentHash
   if (Get-ScheduledTask -TaskName $deploymentTask -ErrorAction SilentlyContinue){throw 'A startup task already exists. Inspect it before changing configuration.'}
   $deploymentArguments='-NoProfile -NonInteractive -WindowStyle Hidden -File "'+(Join-Path $PSScriptRoot 'deploy.ps1')+'" -Action Start'
   if ($Collector){$deploymentArguments+=' -Collector'}
   $deploymentCommand=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $deploymentArguments -WorkingDirectory $PSScriptRoot
   $deploymentTrigger=New-ScheduledTaskTrigger -AtLogOn -User $deploymentUser
   $deploymentPrincipal=New-ScheduledTaskPrincipal -UserId $deploymentUser -LogonType Interactive -RunLevel Limited
   $deploymentSettings=New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 3)
   Register-ScheduledTask -TaskName $deploymentTask -Action $deploymentCommand -Trigger $deploymentTrigger -Principal $deploymentPrincipal -Settings $deploymentSettings -Description 'PrismQuant local logon bootstrap; no operation during shutdown, sleep or sign-out.' | Select-Object TaskName,State
  }
  'UnregisterStartup' {
   $deploymentHash=[System.BitConverter]::ToString([System.Security.Cryptography.SHA256]::Create().ComputeHash([System.Text.Encoding]::UTF8.GetBytes($PSScriptRoot))).Replace('-','').Substring(0,12)
   $deploymentTask='PrismQuant-'+$deploymentHash
   $deploymentRegistered=Get-ScheduledTask -TaskName $deploymentTask -ErrorAction Stop
   if ($deploymentRegistered.Actions.WorkingDirectory -ne $PSScriptRoot){throw 'Startup task ownership differs.'}
   Unregister-ScheduledTask -TaskName $deploymentTask -Confirm:$false
  }
 }
} finally {Pop-Location}
