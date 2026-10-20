$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot
$env:PYTHONPATH = "src"

& ".\.venv\Scripts\python.exe" -m pytest -q -p no:cacheprovider @args
exit $LASTEXITCODE
