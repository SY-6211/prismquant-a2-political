$ErrorActionPreference = 'Stop'
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    throw 'Project Python environment is missing. Follow README.md to create .venv and install dependencies.'
}
Push-Location $PSScriptRoot
try {
    & $projectPython -m app
    if ($LASTEXITCODE -ne 0) { throw "Backend exited with code $LASTEXITCODE" }
}
finally {
    Pop-Location
}
