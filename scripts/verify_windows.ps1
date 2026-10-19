param(
    [switch]$Deep,
    [string]$ConfigPath
)

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $ConfigPath) { $ConfigPath = Join-Path $ProjectRoot 'config.local.json' }
$checks = New-Object System.Collections.Generic.List[object]

function Add-Check {
    param([string]$Name,[string]$Status,[string]$Detail)
    $script:checks.Add([pscustomobject]@{ Check=$Name; Status=$Status; Detail=$Detail }) | Out-Null
}

function First-Line {
    param([string]$Exe,[string[]]$Arguments=@())
    try { $out = & $Exe @Arguments 2>&1 | Select-Object -First 1; if ($out) { return $out.ToString() } } catch { return $null }
}

function Find-LocalExe {
    param([string]$Root,[string]$Name)
    if (Test-Path -LiteralPath $Root) {
        $hit = Get-ChildItem -LiteralPath $Root -Filter $Name -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

function Config-Value {
    param($Config,[string]$Name,$Default)
    if ($Config -and ($Config.PSObject.Properties.Name -contains $Name) -and $Config.$Name) { return $Config.$Name }
    return $Default
}

function Is-Ascii {
    param([string]$Text)
    return -not ($Text.ToCharArray() | Where-Object { [int]$_ -gt 127 })
}

$config = $null
if (Test-Path -LiteralPath $ConfigPath) {
    try { $config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json; Add-Check '設定檔 JSON' 'PASS' $ConfigPath }
    catch { Add-Check '設定檔 JSON' 'FAIL' $_.Exception.Message }
} else { Add-Check '設定檔 JSON' 'FAIL' $ConfigPath }

if (Test-Path -LiteralPath $ProjectRoot) { Add-Check '正式根目錄' 'PASS' $ProjectRoot } else { Add-Check '正式根目錄' 'FAIL' $ProjectRoot }
$workspace = Config-Value $config 'workspace_dir' $ProjectRoot
$rootFull = [System.IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
$workspaceFull = [System.IO.Path]::GetFullPath($workspace)
if ($workspaceFull -eq $rootFull -or $workspaceFull.StartsWith($rootFull + '\',[System.StringComparison]::OrdinalIgnoreCase)) {
    Add-Check 'workspace 邊界' 'PASS' $workspaceFull
} else { Add-Check 'workspace 邊界' 'FAIL' $workspaceFull }

foreach ($relative in @('scripts\run_transcribe.ps1','requirements.txt','config\model-manifest.json','data\custom_terms.txt')) {
    $path = Join-Path $ProjectRoot $relative
    if (Test-Path -LiteralPath $path) { Add-Check $relative 'PASS' $path } else { Add-Check $relative 'FAIL' '不存在' }
}

$desktopInput = Config-Value $config 'desktop_input_dir' (Join-Path ([Environment]::GetFolderPath('Desktop')) '廣播音檔')
$desktopOutput = Config-Value $config 'desktop_output_dir' (Join-Path ([Environment]::GetFolderPath('Desktop')) '廣播文字檔')
$desktopBat = Join-Path (Split-Path -Parent $desktopInput) '開始轉錄.bat'
foreach ($entry in @(
    @{Name='桌面 廣播音檔';Path=$desktopInput},
    @{Name='桌面 廣播文字檔';Path=$desktopOutput},
    @{Name='桌面 開始轉錄.bat';Path=$desktopBat}
)) {
    if (Test-Path -LiteralPath $entry.Path) { Add-Check $entry.Name 'PASS' $entry.Path } else { Add-Check $entry.Name 'FAIL' $entry.Path }
}

if (Test-Path -LiteralPath $desktopBat) {
    $probeStarted = (Get-Date).AddSeconds(-2)
    $previousNoPause = $env:BROADCAST_TRANSCRIBE_NO_PAUSE
    $env:BROADCAST_TRANSCRIBE_NO_PAUSE = '1'
    try {
        & $desktopBat -Once -StableMinutes 0 -OnlyName '__launcher_probe__.wav' 2>&1 | Out-Null
        $probeExit = $LASTEXITCODE
    } finally {
        $env:BROADCAST_TRANSCRIBE_NO_PAUSE = $previousNoPause
    }
    $probeLog = Get-ChildItem -LiteralPath (Join-Path $ProjectRoot 'logs') -Filter 'run_*.log' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -ge $probeStarted } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($probeExit -eq 0 -and $probeLog -and (Get-Content -LiteralPath $probeLog.FullName -Raw -Encoding UTF8) -match '__launcher_probe__') {
        Add-Check '桌面啟動器行為' 'PASS' $probeLog.FullName
    } else {
        Add-Check '桌面啟動器行為' 'FAIL' "exit=$probeExit log=$($probeLog.FullName)"
    }
}

$python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $python) {
    $pyVersion = First-Line $python @('--version')
    & $python -c "from opencc import OpenCC; import importlib.metadata as m; print(m.version('opencc-python-reimplemented')); assert OpenCC('s2tw').convert('后台') == '後臺'" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Add-Check 'Python + OpenCC s2tw' 'PASS' $pyVersion }
    else { Add-Check 'Python + OpenCC s2tw' 'FAIL' '依賴缺失或 s2tw 行為不符' }
} else { Add-Check '專案 Python' 'FAIL' $python }

$ffmpeg = Config-Value $config 'ffmpeg_path' $null
if (-not $ffmpeg -or -not (Test-Path -LiteralPath $ffmpeg)) { $ffmpeg = Find-LocalExe (Join-Path $ProjectRoot 'tools\ffmpeg') 'ffmpeg.exe' }
if ($ffmpeg) { Add-Check 'FFmpeg runtime' 'PASS' ((First-Line $ffmpeg @('-version')) + ' | ' + $ffmpeg) }
else { Add-Check 'FFmpeg runtime' 'FAIL' '找不到 ffmpeg.exe' }

$ffprobe = Config-Value $config 'ffprobe_path' $null
if (-not $ffprobe -and $ffmpeg) { $ffprobe = Join-Path (Split-Path -Parent $ffmpeg) 'ffprobe.exe' }
if (-not $ffprobe -or -not (Test-Path -LiteralPath $ffprobe)) { $ffprobe = Find-LocalExe (Join-Path $ProjectRoot 'tools\ffmpeg') 'ffprobe.exe' }
if ($ffprobe) { Add-Check 'FFprobe runtime' 'PASS' ((First-Line $ffprobe @('-version')) + ' | ' + $ffprobe) }
else { Add-Check 'FFprobe runtime' 'FAIL' '找不到 ffprobe.exe' }

$chunkSeconds = [int](Config-Value $config 'whisper_chunk_seconds' 300)
if ($chunkSeconds -ge 30) { Add-Check 'Whisper 可續跑分段' 'PASS' "$chunkSeconds 秒" }
else { Add-Check 'Whisper 可續跑分段' 'FAIL' 'whisper_chunk_seconds 必須至少為 30' }

$whisper = Config-Value $config 'whisper_cli_path' $null
if (-not $whisper -or -not (Test-Path -LiteralPath $whisper)) { $whisper = Find-LocalExe (Join-Path $ProjectRoot 'tools\whisper.cpp') 'whisper-cli.exe' }
if ($whisper) { Add-Check 'whisper.cpp runtime' 'PASS' ((First-Line $whisper @('--version')) + ' | ' + $whisper) }
else { Add-Check 'whisper.cpp runtime' 'FAIL' '找不到 whisper-cli.exe' }

$modelPath = Config-Value $config 'model_path' (Join-Path $ProjectRoot 'models\ggml-large-v3.bin')
$manifestPath = Join-Path $ProjectRoot 'config\model-manifest.json'
if (Test-Path -LiteralPath $modelPath) {
    $size = (Get-Item -LiteralPath $modelPath).Length
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $expected = ($manifest.models | Where-Object { $_.role -eq 'speech_to_text' } | Select-Object -First 1).sha256
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $modelPath).Hash.ToLowerInvariant()
        if ($size -eq 3095033483 -and $actual -eq $expected) { Add-Check 'Whisper model hash' 'PASS' "$actual | $size bytes" }
        else { Add-Check 'Whisper model hash' 'FAIL' "hash=$actual bytes=$size expected=$expected" }
    } catch { Add-Check 'Whisper model hash' 'FAIL' $_.Exception.Message }
} else { Add-Check 'Whisper model hash' 'FAIL' $modelPath }

$vadModelPath = Config-Value $config 'vad_model_path' (Join-Path $ProjectRoot 'models\ggml-silero-v6.2.0.bin')
if (Test-Path -LiteralPath $vadModelPath) {
    try {
        $vadManifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $vadExpected = ($vadManifest.models | Where-Object { $_.role -eq 'voice_activity_detection' } | Select-Object -First 1).sha256
        $vadActual = (Get-FileHash -Algorithm SHA256 -LiteralPath $vadModelPath).Hash.ToLowerInvariant()
        $vadSize = (Get-Item -LiteralPath $vadModelPath).Length
        if ($vadSize -eq 885098 -and $vadActual -eq $vadExpected) { Add-Check 'Silero VAD model hash' 'PASS' "$vadActual | $vadSize bytes" }
        else { Add-Check 'Silero VAD model hash' 'FAIL' "hash=$vadActual bytes=$vadSize expected=$vadExpected" }
    } catch { Add-Check 'Silero VAD model hash' 'FAIL' $_.Exception.Message }
} else { Add-Check 'Silero VAD model hash' 'FAIL' $vadModelPath }

$ollama = Config-Value $config 'ollama_path' $null
if (-not $ollama -or -not (Test-Path -LiteralPath $ollama)) {
    $ollamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($ollamaCmd) { $ollama = $ollamaCmd.Source }
}
if ($ollama) {
    try {
        $tags = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 10
        $qwen = $tags.models | Where-Object { $_.name -eq 'qwen3:30b' } | Select-Object -First 1
        if ($qwen -and $qwen.digest -eq 'ad815644918f0eaab341c12b67837cc6dd4562342cdaf118f83d5d554cb37226') {
            Add-Check 'Ollama qwen3:30b' 'PASS' $qwen.digest
        } elseif ($qwen) { Add-Check 'Ollama qwen3:30b' 'FAIL' ('digest 不符：' + $qwen.digest) }
        else { Add-Check 'Ollama qwen3:30b' 'WARN' 'API 可用但模型不存在' }
    } catch { Add-Check 'Ollama qwen3:30b' 'WARN' ('API 無法連線：' + $_.Exception.Message) }
} else { Add-Check 'Ollama qwen3:30b' 'WARN' '找不到 Ollama；主轉錄仍可用' }

if ($Deep -and $ffmpeg -and $whisper -and (Test-Path -LiteralPath $modelPath) -and (Test-Path -LiteralPath $vadModelPath)) {
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $deepDir = Join-Path $ProjectRoot (Join-Path 'test_artifacts' ('verify_deep_' + $stamp))
    New-Item -ItemType Directory -Path $deepDir -Force | Out-Null
    $wav = Join-Path $deepDir 'tone.wav'
    $outBase = Join-Path $deepDir 'transcript'
    & $ffmpeg -hide_banner -loglevel error -y -f lavfi -i 'sine=frequency=440:duration=1.2' -ac 1 -ar 16000 -c:a pcm_s16le $wav 2>&1 | Out-Null
    $deepRelative = $deepDir.Substring(($rootFull + '\').Length)
    if (-not (Is-Ascii $deepRelative)) {
        Add-Check 'Whisper deep model load' 'FAIL' "測試路徑不是 ASCII：$deepRelative"
    } else {
        $argsPath = Join-Path $deepDir 'args.rsp'
        $responseArgs = @('-m','models\ggml-large-v3.bin','-f',(Join-Path $deepRelative 'tone.wav'),'-l','en','--vad','-vm','models\ggml-silero-v6.2.0.bin','-otxt','-osrt','-of',(Join-Path $deepRelative 'transcript'))
        [System.IO.File]::WriteAllLines($argsPath,$responseArgs,(New-Object System.Text.UTF8Encoding $false))
        Push-Location -LiteralPath $ProjectRoot
        try { $responseArg = '@' + (Join-Path $deepRelative 'args.rsp'); & $whisper $responseArg 2>&1 | Set-Content -LiteralPath (Join-Path $deepDir 'whisper.log') -Encoding UTF8; $deepExit = $LASTEXITCODE }
        finally { Pop-Location }
        if ($deepExit -eq 0 -and (Test-Path -LiteralPath ($outBase + '.txt')) -and (Test-Path -LiteralPath ($outBase + '.srt'))) {
            Add-Check 'Whisper deep model load' 'PASS' $deepDir
        } else { Add-Check 'Whisper deep model load' 'FAIL' "exit=$deepExit evidence=$deepDir" }
    }
}

$checks | Format-Table -AutoSize -Wrap
if ($checks.Status -contains 'FAIL') { 'RESULT: FAIL'; exit 1 }
if ($checks.Status -contains 'WARN') { 'RESULT: WARN'; exit 0 }
'RESULT: PASS'
exit 0
