# Start AEGIS on Windows. Fully offline: the only thing this contacts is the
# Ollama daemon on this machine. Run .\install.ps1 once first.
#
#   powershell -ExecutionPolicy Bypass -File .\run.ps1
#
[CmdletBinding()]
param([int]$Port = 0)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if ($Port -eq 0) { $Port = if ($env:AEGIS_PORT) { [int]$env:AEGIS_PORT } else { 8501 } }

function Write-Ok   { param([string]$Text) Write-Host "  [OK] $Text" -ForegroundColor Green }
function Write-Warn { param([string]$Text) Write-Host "  [!]  $Text" -ForegroundColor Yellow }
function Stop-Run {
    param([string]$Problem, [string]$Fix)
    Write-Host ""
    Write-Host "ERROR: $Problem" -ForegroundColor Red
    if ($Fix) { Write-Host "Fix: $Fix" -ForegroundColor Yellow }
    Write-Host ""
    exit 1
}

Write-Host "AEGIS - starting" -ForegroundColor White

# -- venv --------------------------------------------------------------------
if (-not (Test-Path 'venv\Scripts\python.exe')) {
    Stop-Run "no virtual environment found." "Run .\install.ps1 first."
}
$py = '.\venv\Scripts\python.exe'
Write-Ok "venv active ($(& $py --version 2>&1))"

# -- Ollama ------------------------------------------------------------------
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Stop-Run "Ollama is not installed." "Run .\install.ps1 first."
}
function Test-OllamaUp {
    try { ollama list 2>&1 | Out-Null; return $LASTEXITCODE -eq 0 } catch { return $false }
}
if (Test-OllamaUp) {
    Write-Ok "Ollama is running"
} else {
    Write-Warn "Ollama is not running - starting it"
    Start-Process -FilePath 'ollama' -ArgumentList 'serve' -WindowStyle Hidden
    $up = $false
    foreach ($i in 1..30) { Start-Sleep -Seconds 1; if (Test-OllamaUp) { $up = $true; break } }
    if (-not $up) {
        Stop-Run "Ollama did not start." "Open a separate PowerShell window and run: ollama serve"
    }
    Write-Ok "Ollama started"
}

# -- Offline verification ----------------------------------------------------
# Ollama was the last thing allowed to need the network (during install). From
# here everything must hold with the network unplugged, so prove it before
# opening the console rather than claiming it in the UI afterwards.
#
# Written to a temp .py file rather than passed via `-c`: Windows PowerShell's
# native-argument quoting mangles a multi-line string containing embedded double
# quotes (a trailing quote/paren gets dropped), which silently truncates a `-c`
# script but never a file path - a file is one argument, no escaping involved.
$offlineCheckScript = @'
from core import offline_check
try:
    r = offline_check.assert_offline()
except offline_check.OfflineViolation as e:
    print(e)
    raise SystemExit(1)
for name in r["checks"]:
    print(f"  [OK] {name}")
for w in r.get("warnings", []):
    print(f"  [!]  {w}")
print(f"  [OK] offline verified - 0 external connections, "
      f"{r['queries_audited']} past queries audited clean")
'@
$offlineCheckPath = Join-Path $env:TEMP "aegis_offline_check_$PID.py"
Set-Content -Path $offlineCheckPath -Value $offlineCheckScript -Encoding UTF8
# Running a .py FILE (rather than -c) puts the file's own directory on sys.path[0],
# not this project's root, so `from core import ...` would fail from a temp
# directory - point PYTHONPATH at the project root for just this one call.
$prevPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = $PSScriptRoot
    & $py $offlineCheckPath
    $offlineCheckExit = $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $prevPythonPath
    # [System.IO.File]::Delete rather than Remove-Item: on some Windows setups the
    # Remove-Item cmdlet itself fails to resolve a %TEMP% path built on an 8.3
    # short name (e.g. "ROCODE~1"), even with -LiteralPath; .NET's own delete
    # doesn't go through that same path-resolution layer.
    try { [System.IO.File]::Delete($offlineCheckPath) } catch { }
}
if ($offlineCheckExit -ne 0) { Stop-Run "offline verification failed - refusing to start." "See the failing check above." }

# -- Index -------------------------------------------------------------------
if (-not (Test-Path 'data\embeddings\index.faiss')) {
    Write-Warn "no RAG index found - building it from docs\"
    & $py -c "from core.rag import build_index; print(f'  indexed {build_index()} chunks')"
    if ($LASTEXITCODE -ne 0) { Stop-Run "index build failed." "Add documents to docs\ and try again." }
}
Write-Ok "knowledge base ready"

# -- Port --------------------------------------------------------------------
$inUse = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($inUse) {
    Stop-Run "port $Port is already in use." "Stop the other process, or run: .\run.ps1 -Port 8502"
}

$url = "http://127.0.0.1:$Port"
Write-Host ""
Write-Host "AEGIS is live -> $url" -ForegroundColor Green
Write-Host ""
Write-Host "Bound to loopback only - not reachable from the plant LAN." -ForegroundColor DarkGray
Write-Host "Chats: data\chats  Index: data\embeddings  Audit log: logs\audit.jsonl" -ForegroundColor DarkGray
Write-Host "Press Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""

# Streamlit runs headless (see .streamlit/config.toml), so open the browser here.
Start-Job -ScriptBlock { Start-Sleep -Seconds 4; Start-Process $using:url } | Out-Null

# Belt-and-braces alongside .streamlit/config.toml - neither alone should be trusted.
$env:STREAMLIT_BROWSER_GATHER_USAGE_STATS = 'false'

& $py -m streamlit run ui/app.py `
    --server.address 127.0.0.1 `
    --server.port $Port `
    --server.headless true `
    --browser.gatherUsageStats false
