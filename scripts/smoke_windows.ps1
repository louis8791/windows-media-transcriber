param([int]$Seconds = 80)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ChunkSeconds = [Math]::Max(30, [int][Math]::Ceiling($Seconds / 2))
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ProductionConfig = Join-Path $ProjectRoot 'config.local.json'
$config = Get-Content -LiteralPath $ProductionConfig -Raw -Encoding UTF8 | ConvertFrom-Json
$ffmpeg = $config.ffmpeg_path
if (-not $ffmpeg -or -not (Test-Path -LiteralPath $ffmpeg)) { throw '找不到正式 FFmpeg runtime' }

$exts = @('.mp3','.wav','.m4a','.mp4','.aac','.flac','.mov','.mkv','.avi','.wmv','.webm','.ogg','.wma','.ts','.mts','.m2ts','.3gp')
$sources = @(Get-ChildItem -LiteralPath $config.desktop_input_dir -File | Where-Object { $exts -contains $_.Extension.ToLowerInvariant() } | Sort-Object Name)
if ($sources.Count -eq 0) { throw '桌面「廣播音檔」沒有可用的真實媒體，無法做 fresh smoke case。' }

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$caseRoot = Join-Path $ProjectRoot (Join-Path 'test_artifacts' ('smoke_' + $stamp))
$inputDir = Join-Path $caseRoot 'input'
$outputDir = Join-Path $caseRoot 'desktop_output'
$workspace = Join-Path $caseRoot 'workspace'
New-Item -ItemType Directory -Path $inputDir,$outputDir,$workspace -Force | Out-Null
$clip = Join-Path $inputDir 'smoke_input.wav'
$selected = $null

foreach ($source in $sources) {
    & $ffmpeg -hide_banner -loglevel error -y -ss 300 -t $Seconds -i $source.FullName -vn -ac 1 -ar 16000 -c:a pcm_s16le $clip 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $clip) -and (Get-Item -LiteralPath $clip).Length -gt 32000) { $selected = $source; break }
    Remove-Item -LiteralPath $clip -Force -ErrorAction SilentlyContinue
}
if (-not $selected) { throw '現有媒體無法擷取含音訊的短測試片段。' }

$smokeConfig = [ordered]@{
    project_root = $ProjectRoot
    workspace_dir = $workspace
    desktop_input_dir = $inputDir
    desktop_output_dir = $outputDir
    desktop_outputs = @('srt','txt','timeline','review','cleaned')
    model_path = $config.model_path
    vad_model_path = $config.vad_model_path
    language = 'zh'
    whisper_chunk_seconds = $ChunkSeconds
    use_ollama_cleanup = 'false'
    ollama_model = 'qwen3:30b'
    ollama_api_url = 'http://127.0.0.1:11434/api/generate'
    ollama_models_dir = $config.ollama_models_dir
    whisper_cli_path = $config.whisper_cli_path
    ffmpeg_path = $config.ffmpeg_path
    ollama_path = $config.ollama_path
}
$smokeConfigPath = Join-Path $caseRoot 'config.json'
$smokeConfig | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $smokeConfigPath -Encoding UTF8

& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'run_transcribe.ps1') -ConfigPath $smokeConfigPath -Once -StableMinutes 0 -OnlyName 'smoke_input.wav' -Force
$pipelineExit = $LASTEXITCODE

# 桌面命名：smoke_input.txt 是含時間碼的正式 SRT 逐位元副本（timeline token）；
# 無時間碼的純文字稿是 smoke_input.純文字.txt（txt token）。
$timecodeTxt = Join-Path $outputDir 'smoke_input.txt'
$plainTxt = Join-Path $outputDir 'smoke_input.純文字.txt'
$srt = Join-Path $outputDir 'smoke_input.srt'
$cleaned = Join-Path $outputDir 'smoke_input.cleaned.txt'
$review = Join-Path $outputDir 'smoke_input.審查報告.txt'
$quality = Join-Path $workspace 'output_final\smoke_input.quality.json'
$finalSrt = Join-Path $workspace 'output_final\smoke_input.srt'
$finalTxt = Join-Path $workspace 'output_final\smoke_input.txt'
$statusFile = Get-ChildItem -LiteralPath (Join-Path $workspace 'logs\status') -Filter '*.json' | Select-Object -First 1
$errors = @()
if ($pipelineExit -ne 0) { $errors += "pipeline exit=$pipelineExit" }
foreach ($required in @($plainTxt,$srt,$timecodeTxt,$quality,$review)) {
    if (-not (Test-Path -LiteralPath $required) -or (Get-Item -LiteralPath $required).Length -eq 0) { $errors += "缺少或空白：$required" }
}
if (-not $statusFile) { $errors += '缺少 status_history JSON' }
if (Test-Path -LiteralPath $srt) {
    $srtText = Get-Content -LiteralPath $srt -Raw -Encoding UTF8
    if ($srtText -notmatch '-->') { $errors += 'SRT 沒有有效 cue' }
    if (-not (Test-Path -LiteralPath $timecodeTxt) -or (Get-Content -LiteralPath $timecodeTxt -Raw -Encoding UTF8) -ne $srtText) {
        $errors += '人可直接開啟的含時間碼 <base>.txt 與 SRT 不一致'
    }
}
if ((Test-Path -LiteralPath $plainTxt) -and (Get-Content -LiteralPath $plainTxt -Raw -Encoding UTF8) -match '-->') {
    $errors += '無時間碼的 <base>.純文字.txt 竟含時間碼，疑似冒充時間碼稿'
}
if (Test-Path -LiteralPath $cleaned) {
    $cleanedText = Get-Content -LiteralPath $cleaned -Raw -Encoding UTF8
    if ($cleanedText -match '<\/?think>|```analysis|```thinking') { $errors += 'cleaned.txt 含推理標記' }
}
if ($statusFile) {
    $status = Get-Content -LiteralPath $statusFile.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($status.current_status -ne 'completed') { $errors += ('job 未完成：' + $status.current_status) }
}

$chunkDir = if ($statusFile) { Join-Path $workspace (Join-Path 'whisper_raw\jobs' (Join-Path $statusFile.BaseName 'chunks')) } else { $null }
$markers = if ($chunkDir -and (Test-Path -LiteralPath $chunkDir)) {
    @(Get-ChildItem -LiteralPath $chunkDir -Filter 'chunk_*.complete' -File | Where-Object { $_.Name -match '^chunk_\d{5}\.complete$' })
} else { @() }
$expectedChunks = [int][Math]::Ceiling($Seconds / [double]$ChunkSeconds)
if ($markers.Count -ne $expectedChunks) { $errors += "分段 checkpoint 數量錯誤：$($markers.Count)/$expectedChunks" }
if ($chunkDir) {
    $responseFiles = @(Get-ChildItem -LiteralPath $chunkDir -Filter 'args_*.rsp' -File -ErrorAction SilentlyContinue)
    foreach ($responseFile in $responseFiles) {
        $responseText = Get-Content -LiteralPath $responseFile.FullName -Raw -Encoding UTF8
        if ($responseText -notmatch '(?m)^--vad\r?$' -or $responseText -notmatch 'ggml-silero-v6\.2\.0\.bin') {
            $errors += "分段未啟用 Silero VAD：$($responseFile.Name)"
        }
        if ($responseText -notmatch '(?m)^-mc\r?\n0\r?$' -or $responseText -notmatch '(?m)^-tpi\r?\n0\.20\r?$') {
            $errors += "分段未啟用防重複解碼：$($responseFile.Name)"
        }
    }
}
if (Test-Path -LiteralPath $plainTxt) {
    $txtText = Get-Content -LiteralPath $plainTxt -Raw -Encoding UTF8
    if ($txtText -match 'CaptionCube') { $errors += 'VAD 後仍出現 CaptionCube 幻覺' }
}
# smoke 驗的是分段合併與 SRT 格式，不是內容品質：只有「重複類」blocking 才算管線失敗，
# 其他內容型 blocking（overlong_cue、hallucination_boilerplate 等）不算失敗，但必須列出供人工判讀。
$repetitionBlockingKinds = @('consecutive_repetition_wall','frequent_exact_phrase')
$nonFatalBlocking = @()
if (Test-Path -LiteralPath $quality) {
    $qualityResult = Get-Content -LiteralPath $quality -Raw -Encoding UTF8 | ConvertFrom-Json
    $qualityFindings = @()
    if ($qualityResult.PSObject.Properties.Name -contains 'review_findings' -and $null -ne $qualityResult.review_findings) {
        $qualityFindings = @($qualityResult.review_findings)
    }
    foreach ($finding in $qualityFindings) {
        if ($null -eq $finding) { continue }
        if ($finding.severity -ne 'blocking') { continue }
        $findingKind = [string]$finding.kind
        $findingRange = "$($finding.start) --> $($finding.end)"
        $findingCues = "cue $($finding.cue_start)-$($finding.cue_end)"
        if ($repetitionBlockingKinds -contains $findingKind) {
            $errors += "fresh smoke 被重複類審查阻擋（$findingKind，$findingRange，$findingCues）"
        } else {
            $nonFatalBlocking += [ordered]@{
                kind = $findingKind
                start = [string]$finding.start
                end = [string]$finding.end
                cue_start = $finding.cue_start
                cue_end = $finding.cue_end
            }
        }
    }
}
if ($nonFatalBlocking.Count -gt 0) {
    Write-Host "NOTE 非重複類 blocking（不算 smoke 失敗，需人工判讀）：" -ForegroundColor Yellow
    foreach ($item in $nonFatalBlocking) {
        Write-Host ("  - {0}｜{1} --> {2}｜cue {3}-{4}" -f $item.kind, $item.start, $item.end, $item.cue_start, $item.cue_end) -ForegroundColor Yellow
    }
}
if (Test-Path -LiteralPath $srt) {
    $mergedSrt = Get-Content -LiteralPath $srt -Raw -Encoding UTF8
    if ($mergedSrt -notmatch '00:00:4\d,\d{3} -->') { $errors += '第二分段沒有加回全域 SRT offset' }
    $canonicalBlocks = @($mergedSrt.TrimEnd([char[]]"`r`n") -split '(?:\r?\n){2}')
    $invalidBlocks = @($canonicalBlocks | Where-Object {
        $_ -notmatch '^\d+\r?\n\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}\r?\n[^\r\n]+$'
    })
    if ($invalidBlocks.Count -gt 0) { $errors += "SRT 未對齊指定單行 cue 格式：$($invalidBlocks.Count)" }
}

$resumeExit = $null
if ($errors.Count -eq 0) {
    $firstMarker = $markers | Sort-Object Name | Select-Object -First 1
    $firstMarkerTime = $firstMarker.LastWriteTimeUtc
    $rawTxt = Join-Path $workspace 'whisper_raw\smoke_input.txt'
    $rawSrt = Join-Path $workspace 'whisper_raw\smoke_input.srt'
    foreach ($generated in @($plainTxt,$srt,$timecodeTxt,$quality,$review,$rawTxt,$rawSrt,$finalTxt,$finalSrt)) {
        if (Test-Path -LiteralPath $generated) { Remove-Item -LiteralPath $generated -Force }
    }
    # Completed media remains registered even after outputs are deleted. This
    # recovery test explicitly requests regeneration while retaining chunks.
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'run_transcribe.ps1') -ConfigPath $smokeConfigPath -Once -StableMinutes 0 -OnlyName 'smoke_input.wav' -RetryCleanup
    $resumeExit = $LASTEXITCODE
    if ($resumeExit -ne 0) { $errors += "resume pipeline exit=$resumeExit" }
    if ((Get-Item -LiteralPath $firstMarker.FullName).LastWriteTimeUtc -ne $firstMarkerTime) { $errors += '續跑時重做了已完成分段' }
    $resumeLog = Get-ChildItem -LiteralPath (Join-Path $workspace 'logs') -Filter 'run_*.log' -File | Sort-Object LastWriteTime | Select-Object -Last 1
    if (-not $resumeLog -or (Get-Content -LiteralPath $resumeLog.FullName -Raw -Encoding UTF8) -notmatch '沿用已完成分段') {
        $errors += '續跑 log 沒有 checkpoint 命中證據'
    }
}

$duplicateExit = $null
if ($errors.Count -eq 0) {
    $duplicate = Join-Path $inputDir 'zz_smoke_duplicate.wav'
    Copy-Item -LiteralPath $clip -Destination $duplicate -Force
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'run_transcribe.ps1') -ConfigPath $smokeConfigPath -Once -StableMinutes 0
    $duplicateExit = $LASTEXITCODE
    if ($duplicateExit -ne 0) { $errors += "duplicate reuse exit=$duplicateExit" }
    $duplicatePlainTxt = Join-Path $outputDir 'zz_smoke_duplicate.純文字.txt'
    $duplicateSrt = Join-Path $outputDir 'zz_smoke_duplicate.srt'
    $duplicateTimecodeTxt = Join-Path $outputDir 'zz_smoke_duplicate.txt'
    foreach ($duplicateOutput in @($duplicatePlainTxt,$duplicateSrt,$duplicateTimecodeTxt)) {
        if (-not (Test-Path -LiteralPath $duplicateOutput) -or (Get-Item -LiteralPath $duplicateOutput).Length -eq 0) {
            $errors += "重複媒體沒有沿用輸出：$duplicateOutput"
        }
    }
    $duplicateLog = Get-ChildItem -LiteralPath (Join-Path $workspace 'logs') -Filter 'run_*.log' -File | Sort-Object LastWriteTime | Select-Object -Last 1
    if (-not $duplicateLog -or (Get-Content -LiteralPath $duplicateLog.FullName -Raw -Encoding UTF8) -notmatch '沿用位元組相同媒體的逐字稿') {
        $errors += '重複媒體沒有命中沿用證據'
    }
}

$singleRunnerExit = $null
if ($errors.Count -eq 0) {
    $probeMutex = New-Object System.Threading.Mutex($false, 'Local\BroadcastTranscribeWindowsSingleRunner')
    $probeAcquired = $false
    try {
        $probeAcquired = $probeMutex.WaitOne(0)
        if (-not $probeAcquired) { $errors += 'smoke 無法取得 single-runner probe mutex' }
        else {
            & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'run_transcribe.ps1') -ConfigPath $smokeConfigPath -Once -OnlyName '__single_runner_probe__'
            $singleRunnerExit = $LASTEXITCODE
            if ($singleRunnerExit -ne 2) { $errors += "第二個 runner 未被拒絕：exit=$singleRunnerExit" }
        }
    }
    finally {
        if ($probeAcquired) { $probeMutex.ReleaseMutex() }
        $probeMutex.Dispose()
    }
}

foreach ($partialName in @('smoke_input.轉錄中.txt','smoke_input.轉錄中.純文字.txt','smoke_input.轉錄中.srt','smoke_input.轉錄中.時間軸.txt','smoke_input.轉錄進度.txt','zz_smoke_duplicate.轉錄中.txt','zz_smoke_duplicate.轉錄中.純文字.txt','zz_smoke_duplicate.轉錄中.srt','zz_smoke_duplicate.轉錄中.時間軸.txt','zz_smoke_duplicate.轉錄進度.txt')) {
    if (Test-Path -LiteralPath (Join-Path $outputDir $partialName)) { $errors += "完成後仍殘留：$partialName" }
}

function Get-DesktopFileBytes {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path) { return (Get-Item -LiteralPath $Path).Length }
    return 0
}
# 先記錄完整交付設定下的桌面檔案大小，之後的預設交付測試會清掉這些檔案。
$plainTxtBytes = Get-DesktopFileBytes $plainTxt
$srtBytes = Get-DesktopFileBytes $srt
$timecodeTxtBytes = Get-DesktopFileBytes $timecodeTxt
$cleanedBytes = Get-DesktopFileBytes $cleaned
$reviewBytes = Get-DesktopFileBytes $review

$defaultDesktopExit = $null
if ($errors.Count -eq 0) {
    # 桌面預設交付行為測試：改用不含 desktop_outputs 的設定重跑，沿用 output_final 既有結果直接進複製階段。
    # 預設 = ["timeline"]，桌面只能出現含時間碼的 <base>.txt，不得出現 .srt 或無時間碼的 .純文字.txt。
    foreach ($existingDesktop in @($plainTxt,$srt,$timecodeTxt,$cleaned,$review)) {
        if (Test-Path -LiteralPath $existingDesktop) { Remove-Item -LiteralPath $existingDesktop -Force }
    }
    # 故意放一個舊命名殘留，驗證預設交付會把它清掉。
    $legacyTimeline = Join-Path $outputDir 'smoke_input.時間軸.txt'
    Set-Content -LiteralPath $legacyTimeline -Value 'legacy residue' -Encoding UTF8
    $defaultConfig = [ordered]@{}
    foreach ($key in @($smokeConfig.Keys)) {
        if ($key -eq 'desktop_outputs') { continue }
        $defaultConfig[$key] = $smokeConfig[$key]
    }
    $defaultConfigPath = Join-Path $caseRoot 'config_default_desktop.json'
    $defaultConfig | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $defaultConfigPath -Encoding UTF8
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'run_transcribe.ps1') -ConfigPath $defaultConfigPath -Once -StableMinutes 0 -OnlyName 'smoke_input.wav' -RetryCleanup
    $defaultDesktopExit = $LASTEXITCODE
    if ($defaultDesktopExit -ne 0) { $errors += "default desktop outputs exit=$defaultDesktopExit" }
    if (-not (Test-Path -LiteralPath $timecodeTxt) -or (Get-Item -LiteralPath $timecodeTxt).Length -eq 0) {
        $errors += '預設設定沒有把含時間碼的 <base>.txt 交付到桌面'
    } else {
        $defaultTimecodeText = Get-Content -LiteralPath $timecodeTxt -Raw -Encoding UTF8
        if ($defaultTimecodeText -notmatch '-->') { $errors += '預設交付的 <base>.txt 沒有時間碼' }
        if (-not (Test-Path -LiteralPath $finalSrt) -or (Get-Content -LiteralPath $finalSrt -Raw -Encoding UTF8) -ne $defaultTimecodeText) {
            $errors += '預設交付的 <base>.txt 與 output_final 的正式 SRT 不一致'
        }
    }
    foreach ($disabled in @($srt,$plainTxt,$cleaned,$legacyTimeline)) {
        if (Test-Path -LiteralPath $disabled) { $errors += "預設設定仍留下非 <base>.txt 的桌面檔：$disabled" }
    }
    $defaultQualityBlocked = $false
    if (Test-Path -LiteralPath $quality) {
        $defaultQuality = Get-Content -LiteralPath $quality -Raw -Encoding UTF8 | ConvertFrom-Json
        $defaultQualityBlocked = ($defaultQuality.content_review_status -eq 'blocked')
    } else {
        $errors += '預設設定沒有產生品質報告'
    }
    if ($defaultQualityBlocked) {
        if (-not (Test-Path -LiteralPath $review)) { $errors += '機器審查 blocked 時桌面缺少審查報告' }
    } elseif (Test-Path -LiteralPath $review) {
        $errors += "預設設定仍留下非 <base>.txt 的桌面檔：$review"
    }
}

$report = [ordered]@{
    verified_at = (Get-Date).ToString('o')
    source_file = $selected.Name
    source_offset_seconds = 300
    clip_seconds = $Seconds
    chunk_seconds = $ChunkSeconds
    chunk_count = $markers.Count
    case_root = $caseRoot
    pipeline_exit = $pipelineExit
    resume_exit = $resumeExit
    duplicate_reuse_exit = $duplicateExit
    single_runner_exit = $singleRunnerExit
    default_desktop_exit = $defaultDesktopExit
    plain_txt_bytes = $plainTxtBytes
    srt_bytes = $srtBytes
    timecode_txt_bytes = $timecodeTxtBytes
    cleaned_bytes = $cleanedBytes
    review_bytes = $reviewBytes
    status = if ($statusFile) { $status.current_status } else { 'missing' }
    non_fatal_blocking = $nonFatalBlocking
    errors = $errors
}
$reportPath = Join-Path $caseRoot 'smoke_report.json'
$report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $reportPath -Encoding UTF8
if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Host "FAIL $_" -ForegroundColor Red }; throw "SMOKE FAIL：$reportPath" }
Write-Host "SMOKE PASS：$reportPath" -ForegroundColor Green
