param(
    [string]$Python = "py"
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectDir

if ($Python -eq "py") {
    & py -3.11 -m pip install --upgrade "pyinstaller>=6.0"
    & py -3.11 -m PyInstaller --noconfirm --clean --windowed --onefile `
        --name "CodexAgentConsole" `
        "$ProjectDir\codex_agent_console.py"
} else {
    & $Python -m pip install --upgrade "pyinstaller>=6.0"
    & $Python -m PyInstaller --noconfirm --clean --windowed --onefile `
        --name "CodexAgentConsole" `
        "$ProjectDir\codex_agent_console.py"
}

Write-Host "Built: $ProjectDir\dist\CodexAgentConsole.exe"
