param([string]$Python = 'python', [switch]$InstallRunner)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
Push-Location -LiteralPath $taskRoot
try {
    & $Python -c 'import sys; assert (3,11) <= sys.version_info[:2] <= (3,12), "AutoCST uses Python 3.11-3.12; 3.11 is verified on this installation"'
    if ($LASTEXITCODE -ne 0) { throw 'Unsupported Python interpreter' }
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        & $Python -m venv --system-site-packages .venv
        if ($LASTEXITCODE -ne 0) { throw 'Could not create virtual environment' }
    }
    & '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Could not install dependencies' }
    & '.\.venv\Scripts\python.exe' -m autocst doctor
    if ($LASTEXITCODE -ne 0) { throw 'Environment inspection failed' }
    if ($InstallRunner) {
        & '.\.venv\Scripts\python.exe' -c 'from pathlib import Path; import sys,json; from autocst.research_service import ResearchService; print(json.dumps(ResearchService(Path(sys.argv[1])).install_runner(),ensure_ascii=False))' $taskRoot
        if ($LASTEXITCODE -ne 0) { throw 'Resident runner installation failed' }
    }
} finally {
    Pop-Location
}
