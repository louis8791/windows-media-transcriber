param([string]$Source)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
function Get-ConfigValue { param($Config, [string]$Name, $Default); if ($Config.PSObject.Properties.Name -contains $Name) { $Config.$Name } else { $Default } }
$config = Get-Content -LiteralPath (Join-Path $ProjectRoot 'config.local.json') -Raw -Encoding UTF8 | ConvertFrom-Json
. (Join-Path $PSScriptRoot 'ensure_ollama.ps1')
$mutex = New-Object Threading.Mutex($false, 'Local\BroadcastTranscribeProofreadOnly')
$held = $false
try {
    try { $held = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $held = $true }
    if (-not $held) { Write-Host 'Another proofreading window is already running.'; exit 2 }
    $null = Ensure-OllamaReady $config
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUTF8 = '1'
    $python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $arguments = @((Join-Path $PSScriptRoot 'proofread_only.py'))
    if ($Source) { $arguments += $Source }
    & $python @arguments
    exit $LASTEXITCODE
} finally {
    if ($held) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
