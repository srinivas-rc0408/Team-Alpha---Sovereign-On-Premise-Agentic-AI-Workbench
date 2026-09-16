# AEGIS one-time setup for Windows (PowerShell 5.1+).
#
# This is the ONLY step that needs internet: it installs Ollama if missing and
# pulls ~6.5GB of model weights. Everything after this runs fully offline.
#
# Safe to re-run: every step checks whether it is already done before doing it.
#
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$Models = @('qwen2.5:7b', 'moondream', 'nomic-embed-text')
$TotalSteps = 10
$script:StepNo = 0

function Write-Step { param([string]$Text)
    $script:StepNo++
    Write-Host ""
    Write-Host "[$script:StepNo/$TotalSteps] $Text" -ForegroundColor White
}
function Write-Ok   { param([string]$Text) Write-Host "  [OK] $Text" -ForegroundColor Green }
function Write-Warn { param([string]$Text) Write-Host "  [!]  $Text" -ForegroundColor Yellow }
function Stop-Install {
    param([string]$Problem, [string]$Fix)
    Write-Host ""
    Write-Host "ERROR at step $script:StepNo : $Problem" -ForegroundColor Red
    if ($Fix) { Write-Host "Fix: $Fix" -ForegroundColor Yellow }
    Write-Host "Nothing was left half-installed - fix the above and re-run .\install.ps1;" -ForegroundColor Yellow
    Write-Host "completed steps are skipped automatically." -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

Write-Host "AEGIS - Air-Gapped Engineering Intelligence System" -ForegroundColor White
Write-Host "One-time setup. Needs internet for this run only." -ForegroundColor DarkGray

# -- 1. OS / architecture ----------------------------------------------------
Write-Step "Detecting OS and architecture"
$arch = $env:PROCESSOR_ARCHITECTURE
$osVersion = [System.Environment]::OSVersion.Version
if ($PSVersionTable.PSVersion.Major -lt 5) {
    Stop-Install "PowerShell $($PSVersionTable.PSVersion) is too old." "Install PowerShell 5.1 or newer (or PowerShell 7: winget install Microsoft.PowerShell)."
}
Write-Ok "Windows $osVersion / $arch / PowerShell $($PSVersionTable.PSVersion)"
if ($arch -notin @('AMD64', 'ARM64')) { Write-Warn "untested architecture '$arch' - continuing anyway" }

# -- 2. Ollama present? ------------------------------------------------------
Write-Step "Checking for Ollama"
function Test-Ollama { $null -ne (Get-Command ollama -ErrorAction SilentlyContinue) }

if (Test-Ollama) {
    Write-Ok "Ollama already installed ($((Get-Command ollama).Source))"
} else {
    Write-Warn "Ollama not found - installing"
    $installed = $false
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "  installing via winget..." -ForegroundColor Yellow
        # winget returns non-zero for benign cases (already installed, agreement prompts),
        # so treat the command's presence on PATH afterwards as the real success test.
        winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements 2>&1 | Out-Null
        $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [System.Environment]::GetEnvironmentVariable('Path', 'User')
        $installed = Test-Ollama
    }
    if (-not $installed) {
        Write-Warn "falling back to the official installer download"
        $setup = Join-Path $env:TEMP 'OllamaSetup.exe'
        try {
            Invoke-WebRequest -Uri 'https://ollama.com/download/OllamaSetup.exe' -OutFile $setup -UseBasicParsing
            Write-Host "  running the Ollama installer (accept any prompts)..." -ForegroundColor Yellow
            Start-Process -FilePath $setup -Wait
        } catch {
            Stop-Install "could not download or run the Ollama installer: $($_.Exception.Message)" `
                         "Install Ollama manually from https://ollama.com/download, then re-run .\install.ps1"
        }
        $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [System.Environment]::GetEnvironmentVariable('Path', 'User')
    }
    if (-not (Test-Ollama)) {
        Stop-Install "Ollama is still not on PATH after installation." `
                     "Close this window, open a NEW PowerShell window (so PATH refreshes), and re-run .\install.ps1"
    }
    Write-Ok "Ollama installed"
}

# -- 3. Ollama service -------------------------------------------------------
Write-Step "Starting the Ollama service"
function Test-OllamaUp {
    try { ollama list 2>&1 | Out-Null; return $LASTEXITCODE -eq 0 } catch { return $false }
}

if (Test-OllamaUp) {
    Write-Ok "Ollama is already running"
} else {
    Start-Process -FilePath 'ollama' -ArgumentList 'serve' -WindowStyle Hidden
    $up = $false
    foreach ($i in 1..30) { Start-Sleep -Seconds 1; if (Test-OllamaUp) { $up = $true; break } }
    if (-not $up) {
        Stop-Install "Ollama did not start within 30 seconds." `
                     "Open a separate PowerShell window, run 'ollama serve', leave it open, then re-run .\install.ps1"
    }
    Write-Ok "Ollama is running"
}

# -- 4. Models ---------------------------------------------------------------
Write-Step "Pulling models (~6.5GB total - the only step that needs internet)"
try {
    $free = (Get-PSDrive -Name ((Get-Location).Drive.Name)).Free / 1GB
    if ($free -lt 10) { Write-Warn ("only {0:N1}GB free on this drive - models need ~8GB" -f $free) }
} catch { }

$present = (ollama list 2>$null | Select-Object -Skip 1 | ForEach-Object { ($_ -split '\s+')[0] })
foreach ($model in $Models) {
    $base = $model.Split(':')[0]
    if ($present -contains $model -or $present -contains "${base}:latest") {
        Write-Ok "$model already present - skipping download"
    } else {
        Write-Host "  downloading $model ..." -ForegroundColor Yellow
        ollama pull $model
        if ($LASTEXITCODE -ne 0) {
            Stop-Install "failed to pull '$model'." `
                         "Check your internet connection and re-run .\install.ps1 - models already downloaded are skipped, so it resumes rather than restarting."
        }
        Write-Ok "$model pulled"
    }
}

# -- 5. venv -----------------------------------------------------------------
Write-Step "Creating the Python virtual environment"
$python = $null
foreach ($candidate in @('python', 'python3', 'py')) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) {
        $version = & $candidate --version 2>&1
        # The Windows Store stub named 'python' prints nothing useful and opens the Store.
        if ($version -match 'Python 3\.(\d+)' -and [int]$Matches[1] -ge 10) { $python = $candidate; break }
    }
}
if (-not $python) {
    Stop-Install "Python 3.10 or newer was not found." `
                 "Install it with: winget install Python.Python.3.12  (tick 'Add python.exe to PATH'), then open a NEW PowerShell window and re-run."
}

if (Test-Path 'venv\Scripts\python.exe') {
    Write-Ok "venv already exists ($(& .\venv\Scripts\python.exe --version 2>&1))"
} else {
    & $python -m venv venv
    if (-not (Test-Path 'venv\Scripts\python.exe')) {
        Stop-Install "could not create the virtual environment." "Check that Python is fully installed (not the Microsoft Store stub), then re-run."
    }
    Write-Ok "venv created ($(& .\venv\Scripts\python.exe --version 2>&1))"
}

# -- 6. Dependencies ---------------------------------------------------------
Write-Step "Installing Python dependencies"
& .\venv\Scripts\python.exe -m pip install --upgrade pip --quiet 2>&1 | Out-Null
$pipOutput = & .\venv\Scripts\pip.exe install -r requirements.txt 2>&1
if ($LASTEXITCODE -ne 0) {
    $pipOutput | Select-Object -Last 20 | ForEach-Object { Write-Host "  $_" }
    Stop-Install "pip install failed (see the output above)." "Resolve the error shown, then re-run .\install.ps1"
}
Write-Ok "dependencies installed"

# -- 7. Local storage --------------------------------------------------------
Write-Step "Creating local storage folders"
foreach ($dir in @('data', 'data\embeddings', 'data\chats', 'logs', 'docs', 'temp')) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}
Write-Ok "data\embeddings, data\chats, logs, docs, temp ready (all local, all gitignored)"

# -- 8. .env -----------------------------------------------------------------
Write-Step "Creating .env from .env.example"
if (Test-Path '.env') {
    Write-Ok ".env already exists - leaving your settings untouched"
} elseif (Test-Path '.env.example') {
    Copy-Item '.env.example' '.env'
    Write-Ok ".env created from .env.example"
} else {
    Write-Warn ".env.example missing - AEGIS will fall back to built-in defaults"
}

# Guarantee the offline/telemetry block regardless of what the template carried.
# core/__init__.py force-sets these at import, so this is documentation parity for
# anyone who reads .env to see what the app does - not the enforcement mechanism.
if ((Test-Path '.env') -and -not (Select-String -Path '.env' -Pattern '^LANGCHAIN_TRACING_V2=' -Quiet)) {
    $envBlock = @'

# -- Telemetry / phone-home switches - all disabled --------------------------
# core/__init__.py force-sets every one of these at import time, before
# langchain/langsmith can read them. Editing these lines cannot re-enable it.
LANGCHAIN_TRACING_V2=false
LANGCHAIN_TRACING=false
LANGSMITH_TRACING=false
LANGCHAIN_ENDPOINT=
ANONYMIZED_TELEMETRY=false
HF_HUB_OFFLINE=1
TRANSFORMERS_OFFLINE=1
SCARF_NO_ANALYTICS=true
DO_NOT_TRACK=1
STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# Local chat history (one JSON file per session)
CHATS_DIR=data/chats
'@
    Add-Content -Path '.env' -Value $envBlock
    Write-Ok "offline/telemetry flags ensured in .env"
}

# -- 9. RAG index ------------------------------------------------------------
Write-Step "Building the RAG index from docs\"
$docs = @(Get-ChildItem -Path 'docs' -Include '*.pdf', '*.txt', '*.md' -Recurse -ErrorAction SilentlyContinue)
if ($docs.Count -gt 0) {
    & .\venv\Scripts\python.exe -c "from core.rag import build_index; print(f'  indexed {build_index()} chunks')"
    if ($LASTEXITCODE -ne 0) {
        Stop-Install "index build failed." "Confirm Ollama is running and nomic-embed-text is pulled, then re-run .\install.ps1"
    }
    Write-Ok "knowledge base indexed into data\embeddings\"
} else {
    Write-Warn "no documents in docs\ - add your SOPs there, then rebuild from the app sidebar"
}

# -- 10. Verify --------------------------------------------------------------
Write-Step "Running the verification suite"
& .\venv\Scripts\python.exe test_aegis.py
if ($LASTEXITCODE -ne 0) {
    Stop-Install "test_aegis.py reported a failure (see the output above)." "Address the failing test, then re-run .\install.ps1"
}

Write-Host ""
Write-Host "AEGIS READY. Run: .\run.ps1" -ForegroundColor Green
Write-Host ""
Write-Host "  .\run.ps1      start the app at http://127.0.0.1:8501"
Write-Host "  .\venv\Scripts\python.exe test_aegis.py    re-run the verification suite"
Write-Host ""
Write-Host "From here on AEGIS needs no internet. You can disconnect the network and it" -ForegroundColor DarkGray
Write-Host "will keep working - models, index, chats and audit log are all on this machine." -ForegroundColor DarkGray
Write-Host ""
