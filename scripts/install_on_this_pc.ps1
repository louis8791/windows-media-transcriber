$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $ProjectRoot 'logs'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$Report = Join-Path $LogDir ('install_on_this_pc_{0}.txt' -f (Get-Date -Format 'yyyyMMdd_HHmmss'))

function Say {
    param([string]$Text)
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Text
    Write-Host $line
    Add-Content -LiteralPath $Report -Value $line -Encoding UTF8
}

function Find-Exe {
    param([string]$Root,[string]$Name)
    if (Test-Path -LiteralPath $Root) {
        $hit = Get-ChildItem -LiteralPath $Root -Filter $Name -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    $cmd = Get-Command ($Name -replace '\.exe$','') -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Install-Ollama-IfMissing {
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $local = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if (Test-Path -LiteralPath $local) { return $local }
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) { Say 'WARN 找不到 Ollama，也找不到 winget，AI 校稿功能暫時不能用。'; return $null }
    Say '找不到 Ollama，嘗試用 winget 安裝 Ollama。'
    & $winget.Source install --id Ollama.Ollama --exact --accept-package-agreements --accept-source-agreements --disable-interactivity --silent 2>&1 | ForEach-Object { Say $_.ToString() }
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    if (Test-Path -LiteralPath $local) { return $local }
    Say 'WARN Ollama 安裝後仍找不到，主轉錄仍可使用。'
    return $null
}

Say "開始設定：$ProjectRoot"
if ($ProjectRoot -ne 'E:\media-transcriber') { Say "WARN 目前不是 E:\media-transcriber，仍會照目前位置設定。" }

foreach ($d in @('input_audio','input_tsp','work_wav','whisper_raw','review_draft','output_final','logs','models','models\ollama','scripts','tools')) {
    New-Item -ItemType Directory -Path (Join-Path $ProjectRoot $d) -Force | Out-Null
}

$Desktop = [Environment]::GetFolderPath('Desktop')
$InputDir = Join-Path $Desktop '音檔'
$OutputDir = Join-Path $Desktop '文字檔'
New-Item -ItemType Directory -Path $InputDir,$OutputDir -Force | Out-Null

$WhisperCli = Find-Exe (Join-Path $ProjectRoot 'tools\whisper.cpp') 'whisper-cli.exe'
$Ffmpeg = Find-Exe (Join-Path $ProjectRoot 'tools\ffmpeg') 'ffmpeg.exe'
$OllamaModels = Join-Path $ProjectRoot 'models\ollama'
[Environment]::SetEnvironmentVariable('OLLAMA_MODELS', $OllamaModels, 'User')
$env:OLLAMA_MODELS = $OllamaModels
$Ollama = Install-Ollama-IfMissing

$VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $VenvPython) {
    Say '安裝文字正規化依賴。'
    & $VenvPython -m pip install -r (Join-Path $ProjectRoot 'requirements.txt') 2>&1 | ForEach-Object { Say $_.ToString() }
} else {
    Say 'FAIL 找不到專案 .venv Python，請先執行 scripts\install_windows.ps1。'
}

$config = [ordered]@{
    project_root = $ProjectRoot
    workspace_dir = $ProjectRoot
    desktop_input_dir = $InputDir
    desktop_output_dir = $OutputDir
    model_path = Join-Path $ProjectRoot 'models\ggml-large-v3.bin'
    vad_model_path = Join-Path $ProjectRoot 'models\ggml-silero-v6.2.0.bin'
    language = 'zh'
    whisper_chunk_seconds = 300
    use_ollama_cleanup = 'auto'
    ollama_model = 'qwen3:30b'
    ollama_api_url = 'http://127.0.0.1:11434/api/generate'
    ollama_models_dir = $OllamaModels
    whisper_cli_path = if ($WhisperCli) { $WhisperCli } else { '' }
    ffmpeg_path = if ($Ffmpeg) { $Ffmpeg } else { '' }
    ollama_path = if ($Ollama) { $Ollama } else { '' }
}
$ExistingConfigPath = Join-Path $ProjectRoot 'config.local.json'
if (Test-Path -LiteralPath $ExistingConfigPath) {
    $ExistingConfig = Get-Content -LiteralPath $ExistingConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($ExistingConfig.PSObject.Properties.Name -contains 'job_identity_input_root') {
        $config.job_identity_input_root = $ExistingConfig.job_identity_input_root
    }
}
$config | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $ProjectRoot 'config.local.json') -Encoding UTF8

$StartBat = [System.IO.File]::ReadAllText((Join-Path $ProjectRoot '開始轉錄.bat'), [System.Text.Encoding]::ASCII)
$Utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText((Join-Path $Desktop '開始轉錄.bat'), $StartBat, $Utf8NoBom)
[System.IO.File]::WriteAllText((Join-Path $ProjectRoot '開始轉錄.bat'), $StartBat, $Utf8NoBom)

$ok = $true
foreach ($item in @(
    @{Name='桌面 音檔'; Path=$InputDir},
    @{Name='桌面 文字檔'; Path=$OutputDir},
    @{Name='開始轉錄.bat'; Path=(Join-Path $Desktop '開始轉錄.bat')},
    @{Name='Whisper 模型'; Path=(Join-Path $ProjectRoot 'models\ggml-large-v3.bin')},
    @{Name='whisper-cli.exe'; Path=$WhisperCli},
    @{Name='ffmpeg.exe'; Path=$Ffmpeg}
    @{Name='專案 Python'; Path=$VenvPython}
)) {
    if ($item.Path -and (Test-Path -LiteralPath $item.Path)) { Say "PASS $($item.Name)：$($item.Path)" }
    else { Say "FAIL $($item.Name)"; $ok = $false }
}

if ($Ollama) {
    $models = & $Ollama list 2>$null
    if (($models -join "`n") -match 'qwen3:30b') { Say 'PASS qwen3:30b：已存在' } else { Say 'WARN qwen3:30b：Ollama 清單看不到，AI 校稿暫時不能用。' }
} else {
    Say 'WARN Ollama：未安裝，AI 校稿暫時不能用。'
}

if ($ok) { Say 'RESULT: PASS，可以雙擊桌面「開始轉錄.bat」使用。'; exit 0 }
Say 'RESULT: FAIL，請把這份 log 傳給管理者。'
exit 1
