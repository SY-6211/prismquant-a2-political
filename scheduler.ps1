param(
    [ValidateSet('check','start','status','stop','logs')]
    [string]$Action = 'status',
    [switch]$AllowEnabled
)
$ErrorActionPreference = 'Stop'
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) { throw 'Project Python environment is missing.' }
Push-Location $PSScriptRoot
try {
    $schedulerArguments = @('-X','utf8','-m','app.scheduler_service',$Action)
    if ($AllowEnabled) { $schedulerArguments += '--allow-enabled' }
    & $projectPython @schedulerArguments
    if ($LASTEXITCODE -ne 0) { throw "Scheduler manager exited with code $LASTEXITCODE" }
}
finally { Pop-Location }
