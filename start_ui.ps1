# One-command launcher for the Web-Scrapper Streamlit UI (Windows / PowerShell).
# Usage:  powershell -NoProfile -ExecutionPolicy Bypass -File .\start_ui.ps1
# Keep this file ASCII-only: Windows PowerShell 5.1 reads BOM-less UTF-8 as ANSI.
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# The default GBK codec on zh-CN Windows cannot decode requirements.txt.
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$venv = Join-Path $root '.venv'
$py = Join-Path $venv 'Scripts\python.exe'

if (-not (Test-Path $py)) {
    Write-Host '==> Creating .venv (reusing globally installed torch / sentence-transformers / chromadb)'
    python -m venv $venv --system-site-packages
    & $py -m pip install --disable-pip-version-check -r requirements.txt
}

# The static fetcher uses urllib and needs an explicit CA bundle on Windows,
# otherwise every https fetch fails with CERTIFICATE_VERIFY_FAILED.
$cert = Join-Path $venv 'Lib\site-packages\certifi\cacert.pem'
if (Test-Path $cert) { $env:SSL_CERT_FILE = $cert }

# Playwright Chromium is required for JS-heavy pages (~150MB on first run).
$browsers = Join-Path $env:USERPROFILE 'AppData\Local\ms-playwright'
if (-not (Get-ChildItem $browsers -Filter 'chromium*' -ErrorAction SilentlyContinue)) {
    Write-Host '==> Installing Playwright Chromium'
    & $py -m playwright install chromium
}

$env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'

Write-Host '==> Starting UI at http://127.0.0.1:8501'
& $py -m streamlit run ui/app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false
