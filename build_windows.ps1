param(
    [string]$Python = "py"
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectDir

if ($Python -eq "py") {
    & py -3.11 -m pip install --upgrade "pyinstaller>=6.0"
    if ($LASTEXITCODE -ne 0) { throw "Failed to install PyInstaller." }
    & py -3.11 -m PyInstaller --noconfirm --clean --windowed --onefile `
        --name "CodexAgentConsole" `
        "$ProjectDir\codex_agent_console.py"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
} else {
    & $Python -m pip install --upgrade "pyinstaller>=6.0"
    if ($LASTEXITCODE -ne 0) { throw "Failed to install PyInstaller." }
    & $Python -m PyInstaller --noconfirm --clean --windowed --onefile `
        --name "CodexAgentConsole" `
        "$ProjectDir\codex_agent_console.py"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
}

Write-Host "Built: $ProjectDir\dist\CodexAgentConsole.exe"
