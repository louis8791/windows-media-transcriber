$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $ProjectRoot 'scripts\run_transcribe.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw $errors[0] }
foreach ($name in @('Get-ConfigValue','Invoke-QwenCleanup')) {
    $fn = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    . ([scriptblock]::Create($fn.Extent.Text))
}
. (Join-Path $ProjectRoot 'scripts\ensure_ollama.ps1')
$config = [pscustomobject]@{ollama_model='qwen3:30b'; ollama_path=$PSHOME+'\powershell.exe'; ollama_models_dir=$ProjectRoot; ollama_api_url='http://127.0.0.1:11434/api/generate'}
$script:requests = 0; $script:starts = 0; $script:mode = 'ready'
function Invoke-RestMethod {
    param($Uri,$TimeoutSec)
    $script:requests++
    if ($script:mode -eq 'cold' -and $script:requests -eq 1) { throw 'connection refused' }
    if ($script:mode -eq 'down') { throw 'connection refused' }
    if ($script:mode -eq 'missing') { return @{models=@()} }
    return @{models=@(@{name='qwen3:30b'})}
}
function Start-Process {
    param($FilePath,$ArgumentList,$WindowStyle,[switch]$PassThru,$RedirectStandardOutput,$RedirectStandardError)
    $script:starts++
    if ($ArgumentList -ne 'serve' -or $WindowStyle -ne 'Hidden' -or $env:OLLAMA_MODELS -ne $ProjectRoot) { throw 'wrong startup contract' }
}
if (-not (Ensure-OllamaReady $config) -or $script:starts -ne 0) { throw 'existing service must be reused' }
$script:mode='cold'; $script:requests=0
$before=$env:OLLAMA_MODELS
if (-not (Ensure-OllamaReady $config) -or $script:starts -ne 1 -or $env:OLLAMA_MODELS -ne $before) { throw 'cold startup failed' }
foreach ($case in @('missing','down')) {
    $script:mode=$case
    $failed=$false
    try { $null=Ensure-OllamaReady $config -WaitSeconds 0 } catch { $failed=$true }
    if (-not $failed) { throw "$case should fail explicitly" }
}
function Invoke-LoggedNative { throw 'must not invoke cleanup after circuit opens' }
$script:CleanupUnavailable='service unavailable'
try { Invoke-QwenCleanup $config 'python' 'input' 'output'; throw 'expected failure' }
catch { if ($_.Exception.Message -ne 'service unavailable') { throw } }
Write-Host 'STARTUP TESTS PASS: reuse, cold start, missing model, offline failure, environment restore, cleanup circuit'
