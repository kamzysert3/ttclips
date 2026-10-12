# One-time setup for ttclips (run from PowerShell)
$ErrorActionPreference = "Stop"
Set-Location "$PSScriptRoot\.."

Write-Host "==> ttclips setup" -ForegroundColor Cyan
$venv = Join-Path $PWD ".venv"

if (-not (Test-Path "$venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow
    python -m venv $venv
}

$py = "$venv\Scripts\python.exe"
$pip = "$venv\Scripts\pip.exe"

Write-Host "Installing (editable) package + Python dependencies..." -ForegroundColor Yellow
& $pip install --upgrade pip
& $pip install -e .

Write-Host "Installing Playwright browser (chromium)..." -ForegroundColor Yellow
& $py -m playwright install chromium

# --- ffmpeg -----------------------------------------------------------
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "ffmpeg not found. Installing via winget..." -ForegroundColor Yellow
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
    # winget portable zips don't always create a PATH shim; add the bin dir
    # to the user PATH so yt-dlp and other tools can find it.
    $ffmpegExe = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe" `
        -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $ffmpegExe) {
        $binDir = $ffmpegExe.DirectoryName
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        if ($userPath -notlike "*$binDir*") {
            [Environment]::SetEnvironmentVariable("Path", "$userPath;$binDir", "User")
            $env:Path = "$env:Path;$binDir"
            Write-Host "Added ffmpeg to PATH: $binDir" -ForegroundColor Green
        }
    }
} else {
    Write-Host "ffmpeg already installed: $((Get-Command ffmpeg).Source)" -ForegroundColor Green
}

# --- .env --------------------------------------------------------------
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from example - EDIT IT and add your GEMINI_API_KEY." -ForegroundColor Magenta
} else {
    Write-Host ".env already exists." -ForegroundColor Green
}

Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. Edit .env  -> set GEMINI_API_KEY, tweak WHISPER_MODEL / TTS_VOICE"
Write-Host "  2. Log into TikTok in Chrome, export cookies to data/cookies.txt (Get cookies.txt LOCALLY extension)"
Write-Host "  3. Test:        .\.venv\Scripts\python.exe -m ttclips --validate-session"
Write-Host "  4. Dry run:     .\.venv\Scripts\python.exe -m ttclips --monitor"
Write-Host "  5. First post:  .\.venv\Scripts\python.exe -m ttclips --once --slot 12:00 --dry-run"
Write-Host "  6. Schedule in Task Scheduler (see README)."