$ErrorActionPreference = "Stop"

Push-Location $PSScriptRoot
try {
    npm.cmd run build
    $pythonPath = Join-Path (Split-Path $PSScriptRoot -Parent) ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        throw "Project virtual environment not found: $pythonPath. Create .venv and install requirements.txt first."
    }
    & $pythonPath -c 'import uiautomator2' 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "uiautomator2 is not installed in $pythonPath. Run: $pythonPath -m pip install -r requirements.txt"
    }

    & $pythonPath server.py
}
finally {
    Pop-Location
}
