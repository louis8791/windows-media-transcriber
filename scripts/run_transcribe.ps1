param(
    [switch]$Force,
    [switch]$Once,
    [switch]$RetryCleanup,
    [int]$StableMinutes = 3,
    [int]$PollSeconds = 15,
    [string]$ConfigPath,
    [string]$OnlyName
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = '1'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $ConfigPath) { $ConfigPath = Join-Path $ProjectRoot 'config.local.json' }
$DefaultLogDir = Join-Path $ProjectRoot 'logs'
New-Item -ItemType Directory -Path $DefaultLogDir -Force | Out-Null
$script:LogPath = Join-Path $DefaultLogDir ('run_{0}.log' -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
$script:HadFailures = $false
$script:MediaHashCache = @{}
$script:ProcessedJobs = @{}
$script:CleanupUnavailable = $null
$script:SelectedInputPaths = @()
$script:BackupStamp = (Get-Date -Format 'yyyyMMdd_HHmmss') + '_' + [guid]::NewGuid().ToString('N').Substring(0,8)
$runnerMutexName = 'Local\BroadcastTranscribeWindowsSingleRunner'
if ($env:BROADCAST_PACKAGE_INSTANCE) { $runnerMutexName += '_' + $env:BROADCAST_PACKAGE_INSTANCE }
$script:RunnerMutex = New-Object System.Threading.Mutex($false, $runnerMutexName)
$script:RunnerMutexAcquired = $false
try { $script:RunnerMutexAcquired = $script:RunnerMutex.WaitOne(0) }
catch [System.Threading.AbandonedMutexException] { $script:RunnerMutexAcquired = $true }
if (-not $script:RunnerMutexAcquired) {
    $message = '已有另一個轉錄視窗正在執行；本次不重複啟動。'
    Write-Host $message -ForegroundColor Yellow
    Add-Content -LiteralPath $script:LogPath -Value $message -Encoding UTF8
    $script:RunnerMutex.Dispose()
    exit 2
}

function Write-RunLog {
    param([string]$Message)
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Write-Host $line
    Add-Content -LiteralPath $script:LogPath -Value $line -Encoding UTF8
}

function Invoke-LoggedNative {
    param(
        [string]$Exe,
        [string[]]$Arguments,
        [string]$Label,
        [string]$WorkingDirectory
    )
    Write-RunLog $Label
    if ($WorkingDirectory) { Push-Location -LiteralPath $WorkingDirectory }
    $previousErrorPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments 2>&1 | ForEach-Object {
            $text = $_.ToString()
            if ($text) {
                Write-Host $text
                Add-Content -LiteralPath $script:LogPath -Value $text -Encoding UTF8
            }
        }
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorPreference
        if ($WorkingDirectory) { Pop-Location }
    }
    if ($exitCode -ne 0) { throw "$Label 失敗，結束碼 $exitCode" }
}

function Get-RequiredConfig {
    if (-not (Test-Path -LiteralPath $ConfigPath)) { throw "找不到設定檔：$ConfigPath" }
    Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Get-ConfigValue {
    param($Config, [string]$Name, $Default)
    if (($Config.PSObject.Properties.Name -contains $Name) -and $null -ne $Config.$Name -and "$($Config.$Name)" -ne '') {
        return $Config.$Name
    }
    return $Default
}

function Get-DesktopOutputTokens {
    # 桌面交付清單；缺少設定鍵時預設只給含時間碼的 <base>.txt（timeline）。
    # output_final 的完整證據不受此設定影響。
    param($Config)
    $allowed = @('srt','txt','timeline','review','cleaned')
    if (($Config.PSObject.Properties.Name -notcontains 'desktop_outputs') -or $null -eq $Config.desktop_outputs) {
        return @('timeline')
    }
    $raw = $Config.desktop_outputs
    if ($raw -is [string] -or $raw -isnot [System.Collections.IEnumerable]) {
        throw "desktop_outputs 必須是字串陣列，例如 [""timeline""]；目前值不是陣列。允許的項目：$($allowed -join '、')"
    }
    $tokens = @()
    foreach ($item in $raw) {
        $token = "$item".Trim().ToLowerInvariant()
        if (-not $token) { throw "desktop_outputs 不得包含空白項目。允許的項目：$($allowed -join '、')" }
        if ($allowed -notcontains $token) {
            throw "desktop_outputs 含未知項目「$token」。允許的項目：$($allowed -join '、')"
        }
        if ($tokens -notcontains $token) { $tokens += $token }
    }
    return $tokens
}

function Resolve-Exe {
    param([string]$Name)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Find-Ffmpeg {
    param($Config)
    $configured = Get-ConfigValue $Config 'ffmpeg_path' $null
    if ($configured -and (Test-Path -LiteralPath $configured)) { return $configured }
    $system = Resolve-Exe 'ffmpeg'
    if ($system) { return $system }
    $found = Get-ChildItem -LiteralPath (Join-Path $ProjectRoot 'tools\ffmpeg') -Filter 'ffmpeg.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.FullName }
    return $null
}

function Find-Ffprobe {
    param($Config, [string]$Ffmpeg)
    $configured = Get-ConfigValue $Config 'ffprobe_path' $null
    if ($configured -and (Test-Path -LiteralPath $configured)) { return $configured }
    if ($Ffmpeg) {
        $sibling = Join-Path (Split-Path -Parent $Ffmpeg) 'ffprobe.exe'
        if (Test-Path -LiteralPath $sibling) { return $sibling }
    }
    $system = Resolve-Exe 'ffprobe'
    if ($system) { return $system }
    $found = Get-ChildItem -LiteralPath (Join-Path $ProjectRoot 'tools\ffmpeg') -Filter 'ffprobe.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.FullName }
    return $null
}

function Find-WhisperCli {
    param($Config)
    $configured = Get-ConfigValue $Config 'whisper_cli_path' $null
    $candidates = @(
        $configured,
        (Join-Path $ProjectRoot 'tools\whisper.cpp\build-vs\bin\Release\whisper-cli.exe'),
        (Join-Path $ProjectRoot 'tools\whisper.cpp\build\bin\Release\whisper-cli.exe')
    ) | Where-Object { $_ }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    $found = Get-ChildItem -LiteralPath (Join-Path $ProjectRoot 'tools\whisper.cpp') -Filter 'whisper-cli.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.FullName }
    return $null
}

function Get-PythonPath {
    $portablePython = Join-Path $ProjectRoot 'runtime\python\python.exe'
    if (Test-Path -LiteralPath $portablePython) { return $portablePython }
    $venvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython) { return $venvPython }
    return $null
}

function Copy-GeneratedFile {
    param([string]$Source, [string]$Destination, [switch]$ForceCopy)
    if ($ForceCopy -or -not (Test-Path -LiteralPath $Destination)) {
        Copy-Item -LiteralPath $Source -Destination $Destination -Force
    }
}

function Get-MediaFiles {
    param([string]$InputDir)
    $exts = @('.mp3','.wav','.m4a','.mp4','.aac','.flac','.mov','.mkv','.avi','.wmv','.webm','.ogg','.wma','.ts','.mts','.m2ts','.3gp')
    Get-ChildItem -LiteralPath $InputDir -File -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $exts -contains $_.Extension.ToLowerInvariant() } |
        Where-Object { -not $OnlyName -or $_.Name -eq $OnlyName } |
        Where-Object { -not $script:SelectedInputPaths.Count -or $script:SelectedInputPaths -contains $_.FullName } |
        Sort-Object FullName
}

function Get-MediaBase {
    param([System.IO.FileInfo]$File, [string]$InputDir = $desktopInput)
    $root = [IO.Path]::GetFullPath($InputDir).TrimEnd('\') + '\'
    $path = [IO.Path]::GetFullPath($File.FullName)
    if (-not $path.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
        throw "媒體不在輸入資料夾內：$path"
    }
    $relative = $path.Substring($root.Length)
    $relative.Substring(0, $relative.Length - $File.Extension.Length)
}

function Get-JobId {
    param([System.IO.FileInfo]$File)
    $material = '{0}|{1}|{2}' -f $File.FullName, $File.Length, $File.LastWriteTimeUtc.Ticks
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { $hash = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($material)) }
    finally { $sha.Dispose() }
    'job_' + (($hash | ForEach-Object { $_.ToString('x2') }) -join '').Substring(0,16)
}

function Test-DeliveredTranscript {
    param([System.IO.FileInfo]$File, [string]$OutputDir)
    # Exact base-name only. Review/cleaned/progress/partial files never match.
    $base = Get-MediaBase $File
    foreach ($extension in @('.txt','.srt')) {
        $candidate = Join-Path $OutputDir ($base + $extension)
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            if ((Get-Item -LiteralPath $candidate).Length -gt 0) { return $true }
        }
    }
    # A moved transcript is still delivered. Do not guess across duplicate input names.
    $otherNames = @(Get-ChildItem -LiteralPath $desktopInput -File -Recurse -ErrorAction Stop |
        Where-Object { $_.BaseName -eq $File.BaseName -and $_.FullName -ne $File.FullName })
    if ($otherNames.Count -gt 0) { return $false }
    # ponytail: scan on demand; index only if large output trees make polling slow.
    $moved = Get-ChildItem -LiteralPath $OutputDir -File -Recurse -ErrorAction Stop |
        Where-Object { $_.BaseName -eq $File.BaseName -and $_.Extension.ToLowerInvariant() -in @('.txt','.srt') -and $_.Length -gt 0 } |
        Select-Object -First 1
    if ($moved) {
        Write-RunLog ('已找到移入子資料夾的正式稿，保留原位置、不補回根目錄：' + $moved.FullName)
        return $true
    }
    return $false
}

function Get-MediaFingerprint {
    param([System.IO.FileInfo]$File)
    $key = '{0}|{1}|{2}' -f $File.FullName, $File.Length, $File.LastWriteTimeUtc.Ticks
    if (-not $script:MediaHashCache.ContainsKey($key)) {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $stream = [System.IO.File]::OpenRead($File.FullName)
        try { $hash = $sha.ComputeHash($stream) }
        finally { $stream.Dispose(); $sha.Dispose() }
        $script:MediaHashCache[$key] = (($hash | ForEach-Object { $_.ToString('x2') }) -join '')
    }
    return $script:MediaHashCache[$key]
}

function Backup-TranscriptsForRetranscription {
    param([System.IO.FileInfo]$File, [string]$Workspace, [string]$OutputDir)
    $base = Get-MediaBase $File
    $backupRoot = Join-Path $Workspace ('work\retranscribe_backups\' + $script:BackupStamp)
    foreach ($entry in @(@{Name='desktop'; Path=$OutputDir},
        @{Name='output_final'; Path=(Join-Path $Workspace 'output_final')},
        @{Name='whisper_raw'; Path=(Join-Path $Workspace 'whisper_raw')})) {
        foreach ($suffix in @('.txt','.srt','.純文字.txt','.cleaned.txt','.審查報告.txt','.quality.json','.重查比對.txt','.recheck.json')) {
            $source = Join-Path $entry.Path ($base + $suffix)
            if (Test-Path -LiteralPath $source -PathType Leaf) {
                $target = Join-Path (Join-Path $backupRoot $entry.Name) ($base + $suffix)
                New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
                Copy-Item -LiteralPath $source -Destination $target -ErrorAction Stop
            }
        }
    }
    if (Test-Path -LiteralPath $backupRoot) { Write-RunLog ('更新前已備份舊稿：' + $backupRoot) }
}

function Write-CompletedMedia {
    param([System.IO.FileInfo]$File, [string]$Workspace, [string]$Evidence)
    $fingerprint = Get-MediaFingerprint $File
    $dir = Join-Path $Workspace 'logs\completed_media'
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    $path = Join-Path $dir ($fingerprint + '.json')
    $inputPaths = @($File.FullName)
    if (Test-Path -LiteralPath $path) {
        $previous = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
        $inputPaths = @(@($previous.input_paths) + $inputPaths | Where-Object { $_ } | Select-Object -Unique)
    }
    $record = [ordered]@{
        schema_version = 1; media_sha256 = $fingerprint; input_path = $File.FullName
        input_paths = $inputPaths
        completed_at = (Get-Date).ToString('o'); evidence = $Evidence
    }
    $record | ConvertTo-Json | Set-Content -LiteralPath ($path + '.tmp') -Encoding UTF8
    Move-Item -LiteralPath ($path + '.tmp') -Destination $path -Force
}

function Test-CompletedMedia {
    param([System.IO.FileInfo]$File, [string]$Workspace, [string]$OutputDir)
    $fingerprint = Get-MediaFingerprint $File
    $path = Join-Path $Workspace ('logs\completed_media\' + $fingerprint + '.json')
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        $record = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($record.schema_version -ne 1 -or $record.media_sha256 -ne $fingerprint -or
            -not $record.completed_at -or -not @($record.input_paths | Where-Object { $_ }).Count) {
            throw "轉錄完成紀錄無效，請先檢查：$path"
        }
        # A newly added copy still gets its own delivery, as before. A renamed
        # source (prior path gone) or already registered source stays skipped.
        $knownPaths = @($record.input_paths)
        if ($knownPaths -contains $File.FullName) { return $true }
        if (-not @($knownPaths | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }).Count) {
            Write-CompletedMedia $File $Workspace $record.evidence
            return $true
        }
    }
    # A replaced source must never borrow the old same-name transcript.
    $receiptDir = Join-Path $Workspace 'logs\completed_media'
    if (Test-Path -LiteralPath $receiptDir) {
        foreach ($receipt in Get-ChildItem -LiteralPath $receiptDir -Filter '*.json' -File) {
            $prior = Get-Content -LiteralPath $receipt.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
            if (@($prior.input_paths) -contains $File.FullName -and $prior.media_sha256 -ne $fingerprint) {
                throw "同路徑音檔內容已更換，不能沿用舊同名稿；請替新音檔改名或明確使用 -Force：$($File.FullName)"
            }
        }
    }
    # Legacy completion is trusted only with a completed job, full outputs,
    # and an archived source whose bytes still match the current media.
    $base = Get-MediaBase $File
    $statusPath = Join-Path $Workspace ('logs\status\' + (Get-JobId $File) + '.json')
    $backup = Join-Path $Workspace ('input_audio\' + $base + $File.Extension)
    $finalTxt = Join-Path $Workspace ('output_final\' + $base + '.txt')
    $finalSrt = Join-Path $Workspace ('output_final\' + $base + '.srt')
    if ((Test-Path -LiteralPath $statusPath -PathType Leaf) -and
        (Test-Path -LiteralPath $backup -PathType Leaf) -and
        (Test-Path -LiteralPath $finalTxt -PathType Leaf) -and
        (Test-Path -LiteralPath $finalSrt -PathType Leaf)) {
        $status = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($status.current_status -in @('completed','completed_with_warning') -and
            (Get-Item -LiteralPath $finalTxt).Length -gt 0 -and
            (Get-Item -LiteralPath $finalSrt).Length -gt 0 -and
            (Get-MediaFingerprint (Get-Item -LiteralPath $backup)) -eq $fingerprint) {
            Write-CompletedMedia $File $Workspace 'verified_legacy_completion'
            return $true
        }
    }
    # Retain the existing exact-name/manual-delivery contract and register it
    # once, so later transcript renames no longer depend on the original name.
    if (Test-DeliveredTranscript $File $OutputDir) {
        Write-CompletedMedia $File $Workspace 'existing_delivered_transcript'
        return $true
    }
    return $false
}

function Get-AsciiRelativePath {
    param([string]$Path)
    $fullRoot = [System.IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\') + '\'
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $fullPath.StartsWith($fullRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Whisper 內部路徑不在正式專案根目錄內：$fullPath"
    }
    $relative = $fullPath.Substring($fullRoot.Length)
    if ($relative.ToCharArray() | Where-Object { [int]$_ -gt 127 }) {
        throw "Whisper 內部路徑必須是 ASCII：$relative"
    }
    return $relative
}

function Get-MediaDurationSeconds {
    param([string]$Ffprobe, [string]$MediaPath)
    $previousErrorPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $Ffprobe -v error -show_entries format=duration -of 'default=noprint_wrappers=1:nokey=1' $MediaPath 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previousErrorPreference }
    if ($exitCode -ne 0) { throw "FFprobe 讀取媒體長度失敗，結束碼 $exitCode" }
    $durationText = ($output | Select-Object -Last 1).ToString().Trim()
    $duration = 0.0
    if (-not [double]::TryParse($durationText, [System.Globalization.NumberStyles]::Float, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$duration)) {
        throw "FFprobe 回傳無效長度：$durationText"
    }
    if ($duration -le 0) { throw "媒體長度必須大於 0 秒：$durationText" }
    return $duration
}

function Write-JobStatus {
    param(
        [string]$StatusPath,
        [string]$JobId,
        [string]$InputPath,
        [string]$Status,
        [string]$Detail = ''
    )
    if (Test-Path -LiteralPath $StatusPath) {
        $record = Get-Content -LiteralPath $StatusPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $history = @($record.status_history)
    } else {
        $record = [ordered]@{ schema_version = 1; job_id = $JobId; input_path = $InputPath; status_history = @() }
        $history = @()
    }
    $history += [ordered]@{ at = (Get-Date).ToString('o'); status = $Status; detail = $Detail }
    $record.status_history = $history
    $record.current_status = $Status
    $temp = $StatusPath + '.tmp'
    $record | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temp -Encoding UTF8
    Move-Item -LiteralPath $temp -Destination $StatusPath -Force
}

function Mark-PriorRunInterrupted {
    param([string]$StatusPath, [string]$JobId, [string]$InputPath)
    if (-not (Test-Path -LiteralPath $StatusPath)) { return }
    $record = Get-Content -LiteralPath $StatusPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (@('completed','completed_with_warning','failed','interrupted') -notcontains $record.current_status) {
        Write-JobStatus $StatusPath $JobId $InputPath 'interrupted' '前次程序未正常結束；本次將從已完成分段續跑'
    }
}

function Test-CompletedChunk {
    param([string]$ChunkBase, [string]$ExpectedSignature)
    $marker = $ChunkBase + '.complete'
    return (Test-Path -LiteralPath $marker) -and
        (Test-Path -LiteralPath ($ChunkBase + '.txt')) -and
        (Test-Path -LiteralPath ($ChunkBase + '.srt')) -and
        ((Get-Content -LiteralPath $marker -Raw -Encoding ASCII).Trim() -eq $ExpectedSignature)
}

function Merge-WhisperChunks {
    param(
        [string]$Python,
        [string]$ChunkDir,
        [int]$ExpectedCount,
        [string]$OutputTxt,
        [string]$OutputSrt,
        [int]$ChunkSeconds,
        [string]$Label
    )
    Invoke-LoggedNative -Exe $Python -Arguments @(
        (Join-Path $PSScriptRoot 'merge_whisper_chunks.py'),
        $ChunkDir,
        "$ExpectedCount",
        $OutputTxt,
        $OutputSrt,
        "$ChunkSeconds"
    ) -Label $Label -WorkingDirectory $ProjectRoot
}

function Write-TranscriptionProgress {
    param([string]$ProgressPath, [int]$Completed, [int]$Total)
    $message = @(
        '影片正在轉錄，尚未完成。',
        "已完成分段：$Completed / $Total",
        '請保持「Broadcast Transcription」視窗開啟。若視窗中止，下次啟動會從已完成分段續跑。',
        ('更新時間：' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
    ) -join [Environment]::NewLine
    Set-Content -LiteralPath $ProgressPath -Value $message -Encoding UTF8
}

function Write-QueuedTranscriptionProgress {
    param([string]$ProgressPath, [int]$Position, [int]$Total, [string]$DuplicateOf = '')
    New-Item -ItemType Directory -Path (Split-Path -Parent $ProgressPath) -Force | Out-Null
    $detail = if ($DuplicateOf) {
        "內容與「$DuplicateOf」相同；前一份完成後直接沿用，不會重跑辨識。"
    } else {
        '前方影片完成後會自動開始，不需要再次按啟動器。'
    }
    $message = @(
        '影片已排入轉錄佇列，尚未漏轉。',
        "目前順位：$Position / $Total",
        $detail,
        ('更新時間：' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
    ) -join [Environment]::NewLine
    Set-Content -LiteralPath $ProgressPath -Value $message -Encoding UTF8
}

function Test-OllamaCleanupEnabled {
    param($Config)
    $mode = "$(Get-ConfigValue $Config 'use_ollama_cleanup' 'auto')".ToLowerInvariant()
    return @('false','off','disabled','never','0') -notcontains $mode
}

function Invoke-QwenCleanup {
    param(
        $Config,
        [string]$Python,
        [string]$FinalTxt,
        [string]$CleanedTxt
    )
    if ($script:CleanupUnavailable) { throw $script:CleanupUnavailable }
    $model = Get-ConfigValue $Config 'ollama_model' 'qwen3:8b'
    $apiUrl = Get-ConfigValue $Config 'ollama_api_url' 'http://127.0.0.1:11434/api/generate'
    $terms = Join-Path $ProjectRoot 'data\custom_terms.txt'
    $args = @(
        (Join-Path $PSScriptRoot 'clean_transcript.py'), $FinalTxt, $CleanedTxt,
        '--model', $model, '--api-url', $apiUrl, '--terms', $terms,
        '--max-chars', "$(Get-ConfigValue $Config 'ollama_chunk_chars' 900)",
        '--context-size', "$(Get-ConfigValue $Config 'ollama_context_size' 8192)"
    )
    Invoke-LoggedNative -Exe $Python -Arguments $args -Label 'Qwen 本機校稿中' -WorkingDirectory $ProjectRoot
}

function Process-MediaFile {
    param(
        [System.IO.FileInfo]$File,
        $Config,
        [string]$Workspace,
        [string]$Ffmpeg,
        [string]$Ffprobe,
        [string]$WhisperCli,
        [string]$Python,
        [string]$ReuseFromBase = ''
    )

    $base = Get-MediaBase $File
    $jobId = Get-JobId $File
    $inputAudioDir = Join-Path $Workspace 'input_audio'
    $workWavDir = Join-Path $Workspace 'work_wav'
    $rawDir = Join-Path $Workspace 'whisper_raw'
    $finalDir = Join-Path $Workspace 'output_final'
    $statusDir = Join-Path $Workspace 'logs\status'
    $desktopOut = Get-ConfigValue $Config 'desktop_output_dir' (Join-Path ([Environment]::GetFolderPath('Desktop')) '廣播文字檔')
    $desktopOutputs = Get-DesktopOutputTokens $Config
    foreach ($dir in @($inputAudioDir,$workWavDir,$rawDir,$finalDir,$statusDir,$desktopOut)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    foreach ($dir in @($inputAudioDir,$rawDir,$finalDir,$desktopOut)) {
        New-Item -ItemType Directory -Path (Split-Path -Parent (Join-Path $dir ($base + '.txt'))) -Force | Out-Null
    }

    $statusPath = Join-Path $statusDir ($jobId + '.json')
    $finalTxt = Join-Path $finalDir ($base + '.txt')
    $finalSrt = Join-Path $finalDir ($base + '.srt')
    $cleanedTxt = Join-Path $finalDir ($base + '.cleaned.txt')
    $qualityPath = Join-Path $finalDir ($base + '.quality.json')
    $reviewPath = Join-Path $finalDir ($base + '.審查報告.txt')
    $recheckPath = Join-Path $finalDir ($base + '.重查比對.txt')
    $recheckJson = Join-Path $finalDir ($base + '.recheck.json')
    $desktopRecheck = Join-Path $desktopOut ($base + '.重查比對.txt')
    # 桌面命名：<base>.txt 是含時間碼的正式 SRT 逐位元副本（timeline）；
    # 無時間碼的純文字稿讓出這個名字，改叫 <base>.純文字.txt（txt）。兩者不得互相冒充。
    $desktopTimecodeTxt = Join-Path $desktopOut ($base + '.txt')
    $desktopPlainTxt = Join-Path $desktopOut ($base + '.純文字.txt')
    $desktopSrt = Join-Path $desktopOut ($base + '.srt')
    $desktopCleaned = Join-Path $desktopOut ($base + '.cleaned.txt')
    $desktopReview = Join-Path $desktopOut ($base + '.審查報告.txt')
    $desktopPartialTimecodeTxt = Join-Path $desktopOut ($base + '.轉錄中.txt')
    $desktopPartialPlainTxt = Join-Path $desktopOut ($base + '.轉錄中.純文字.txt')
    $desktopPartialSrt = Join-Path $desktopOut ($base + '.轉錄中.srt')
    $desktopProgress = Join-Path $desktopOut ($base + '.轉錄進度.txt')
    # 舊命名殘留：已不再產生，但桌面可能還留著上一版的檔，必須清乾淨避免新舊混雜。
    $legacyDesktopTimelineTxt = Join-Path $desktopOut ($base + '.時間軸.txt')
    $legacyDesktopPartialTimelineTxt = Join-Path $desktopOut ($base + '.轉錄中.時間軸.txt')
    Mark-PriorRunInterrupted $statusPath $jobId $File.FullName
    Write-JobStatus $statusPath $jobId $File.FullName 'queued' '已進入處理佇列'

    try {
        if ($Force -or (Get-ConfigValue $Config 'backup_before_delivery' $false)) {
            Backup-TranscriptsForRetranscription $File $Workspace $desktopOut
        }
        $needsAsr = $Force -or -not ((Test-Path -LiteralPath $finalTxt) -and (Test-Path -LiteralPath $finalSrt))
        $shouldCleanup = $needsAsr -or $RetryCleanup
        if ($needsAsr -and -not $Force -and $ReuseFromBase) {
            $reuseFinalTxt = Join-Path $finalDir ($ReuseFromBase + '.txt')
            $reuseFinalSrt = Join-Path $finalDir ($ReuseFromBase + '.srt')
            if ((Test-Path -LiteralPath $reuseFinalTxt) -and (Test-Path -LiteralPath $reuseFinalSrt)) {
                Write-JobStatus $statusPath $jobId $File.FullName 'reusing_identical_media' "沿用位元組相同媒體：$ReuseFromBase"
                Copy-Item -LiteralPath $reuseFinalTxt -Destination $finalTxt -Force
                Copy-Item -LiteralPath $reuseFinalSrt -Destination $finalSrt -Force
                $reuseRawTxt = Join-Path $rawDir ($ReuseFromBase + '.txt')
                $reuseRawSrt = Join-Path $rawDir ($ReuseFromBase + '.srt')
                $reuseCleanedTxt = Join-Path $finalDir ($ReuseFromBase + '.cleaned.txt')
                if (Test-Path -LiteralPath $reuseRawTxt) { Copy-Item -LiteralPath $reuseRawTxt -Destination (Join-Path $rawDir ($base + '.txt')) -Force }
                if (Test-Path -LiteralPath $reuseRawSrt) { Copy-Item -LiteralPath $reuseRawSrt -Destination (Join-Path $rawDir ($base + '.srt')) -Force }
                if (Test-Path -LiteralPath $reuseCleanedTxt) { Copy-Item -LiteralPath $reuseCleanedTxt -Destination $cleanedTxt -Force }
                foreach ($extension in @('.重查比對.txt','.recheck.json')) {
                    $reuseReview = Join-Path $finalDir ($ReuseFromBase + $extension)
                    if (Test-Path -LiteralPath $reuseReview) { Copy-Item -LiteralPath $reuseReview -Destination (Join-Path $finalDir ($base + $extension)) -Force }
                }
                Write-RunLog "沿用位元組相同媒體的逐字稿：$ReuseFromBase -> $base"
                $needsAsr = $false
            }
        }
        if ($needsAsr -and (Get-ConfigValue $Config 'reuse_only' $false)) {
            throw '沒有可沿用的完整辨識結果，請改選「一般轉錄」或「重新轉錄」。'
        }
        if ($needsAsr) {
            Write-RunLog "開始處理：$($File.Name)"
            Write-JobStatus $statusPath $jobId $File.FullName 'archiving_input' '保存原始輸入副本'
            $inputCopy = Join-Path $inputAudioDir ($base + $File.Extension)
            if (Test-Path -LiteralPath $inputCopy) {
                $existing = Get-Item -LiteralPath $inputCopy
                if ($existing.Length -ne $File.Length) {
                    $inputCopy = Join-Path $inputAudioDir ('{0}_{1}{2}' -f $base, $jobId, $File.Extension)
                }
            }
            if ($Force -or -not (Test-Path -LiteralPath $inputCopy)) {
                Copy-Item -LiteralPath $File.FullName -Destination $inputCopy -Force:$Force
            }

            Write-JobStatus $statusPath $jobId $File.FullName 'converting_audio' 'FFmpeg 16 kHz mono WAV'
            $wavPath = Join-Path $workWavDir ($jobId + '.wav')
            $wavMarker = $wavPath + '.complete'
            if (-not $Force -and (Test-Path -LiteralPath $wavPath) -and -not (Test-Path -LiteralPath $wavMarker)) {
                $sourceDuration = Get-MediaDurationSeconds $Ffprobe $File.FullName
                $existingWavDuration = Get-MediaDurationSeconds $Ffprobe $wavPath
                if ([Math]::Abs($sourceDuration - $existingWavDuration) -le 1.0) {
                    Set-Content -LiteralPath $wavMarker -Value (Get-Date).ToString('o') -Encoding ASCII
                }
            }
            if ($Force -or -not ((Test-Path -LiteralPath $wavPath) -and (Test-Path -LiteralPath $wavMarker))) {
                $wavTemp = $wavPath + '.partial.wav'
                if (Test-Path -LiteralPath $wavTemp) { Remove-Item -LiteralPath $wavTemp -Force }
                Invoke-LoggedNative -Exe $Ffmpeg -Arguments @('-hide_banner','-loglevel','warning','-y','-i',$File.FullName,'-vn','-ac','1','-ar','16000','-c:a','pcm_s16le',$wavTemp) -Label "轉成 Whisper 用 WAV：$($File.Name)"
                Move-Item -LiteralPath $wavTemp -Destination $wavPath -Force
                Set-Content -LiteralPath $wavMarker -Value (Get-Date).ToString('o') -Encoding ASCII
            } else {
                Write-RunLog "沿用已完成的 WAV：$($File.Name)"
            }

            $jobDir = Join-Path $rawDir (Join-Path 'jobs' $jobId)
            $chunkDir = Join-Path $jobDir 'chunks'
            New-Item -ItemType Directory -Path $chunkDir -Force | Out-Null
            $durationSeconds = Get-MediaDurationSeconds $Ffprobe $wavPath
            $chunkSeconds = [int](Get-ConfigValue $Config 'whisper_chunk_seconds' 300)
            if ($chunkSeconds -lt 30) { throw 'whisper_chunk_seconds 必須至少為 30，避免 Whisper 視窗跨段重疊' }
            $totalChunks = [int][Math]::Ceiling($durationSeconds / $chunkSeconds)
            $modelPath = Get-ConfigValue $Config 'model_path' (Join-Path $ProjectRoot 'models\ggml-large-v3.bin')
            $vadModelPath = Get-ConfigValue $Config 'vad_model_path' (Join-Path $ProjectRoot 'models\ggml-silero-v6.2.0.bin')
            $language = Get-ConfigValue $Config 'language' 'zh'
            $asrCheckpointSignature = 'asr-v2|model={0}|language={1}|vad=true|mc=0|tpi=0.20' -f ([System.IO.Path]::GetFileName($modelPath)), $language
            if ($language -eq 'en') { $asrCheckpointSignature += '|prompt=english-original-v1' }
            if (-not (Test-Path -LiteralPath $vadModelPath)) { throw "找不到 Silero VAD 模型：$vadModelPath" }
            Write-JobStatus $statusPath $jobId $File.FullName 'whisper_transcribing' ("whisper.cpp $([System.IO.Path]::GetFileName($modelPath))；共 $totalChunks 個可續跑分段；防重複解碼已啟用")
            Write-TranscriptionProgress $desktopProgress 0 $totalChunks
            $termsPath = Join-Path $ProjectRoot 'data\custom_terms.txt'
            $terms = @()
            if (Test-Path -LiteralPath $termsPath) {
                $terms = @(Get-Content -LiteralPath $termsPath -Encoding UTF8 | Where-Object { $_.Trim() -and -not $_.TrimStart().StartsWith('#') })
            }
            $utf8NoBom = New-Object System.Text.UTF8Encoding $false
            for ($chunkIndex = 0; $chunkIndex -lt $totalChunks; $chunkIndex++) {
                $chunkNumber = $chunkIndex + 1
                $chunkBase = Join-Path $chunkDir ('chunk_{0:D5}' -f $chunkIndex)
                $chunkMarker = $chunkBase + '.complete'
                if ($Force) {
                    foreach ($stale in @($chunkMarker,($chunkBase + '.txt'),($chunkBase + '.srt'),($chunkBase + '.wav'),($chunkBase + '.wav.complete'))) {
                        if (Test-Path -LiteralPath $stale) { Remove-Item -LiteralPath $stale -Force }
                    }
                }
                if (Test-CompletedChunk $chunkBase $asrCheckpointSignature) {
                    Write-RunLog "沿用已完成分段：$chunkNumber / $totalChunks"
                } else {
                    $offsetMs = [int64]$chunkIndex * [int64]$chunkSeconds * 1000
                    $remainingMs = [int64][Math]::Ceiling(($durationSeconds * 1000) - $offsetMs)
                    $durationMs = [Math]::Min(([int64]$chunkSeconds * 1000), $remainingMs)
                    $chunkWav = $chunkBase + '.wav'
                    $chunkWavMarker = $chunkWav + '.complete'
                    if (-not ((Test-Path -LiteralPath $chunkWav) -and (Test-Path -LiteralPath $chunkWavMarker))) {
                        $chunkWavTemp = $chunkBase + '.partial.wav'
                        if (Test-Path -LiteralPath $chunkWavTemp) { Remove-Item -LiteralPath $chunkWavTemp -Force }
                        $offsetSecondsText = ([double]($offsetMs / 1000.0)).ToString('0.###', [System.Globalization.CultureInfo]::InvariantCulture)
                        $durationSecondsText = ([double]($durationMs / 1000.0)).ToString('0.###', [System.Globalization.CultureInfo]::InvariantCulture)
                        Invoke-LoggedNative -Exe $Ffmpeg -Arguments @('-hide_banner','-loglevel','warning','-y','-ss',$offsetSecondsText,'-t',$durationSecondsText,'-i',$wavPath,'-c:a','copy',$chunkWavTemp) -Label "建立語音偵測分段：$chunkNumber / $totalChunks"
                        Move-Item -LiteralPath $chunkWavTemp -Destination $chunkWav -Force
                        Set-Content -LiteralPath $chunkWavMarker -Value (Get-Date).ToString('o') -Encoding ASCII
                    }
                    $responsePath = Join-Path $chunkDir ('args_{0:D5}.rsp' -f $chunkIndex)
                    $responseArgs = @(
                        '-m', (Get-AsciiRelativePath $modelPath),
                        '-f', (Get-AsciiRelativePath $chunkWav),
                        '-l', $language,
                        '-mc', '0', '-tpi', '0.20',
                        '--vad', '-vm', (Get-AsciiRelativePath $vadModelPath),
                        '-otxt', '-osrt', '-of', (Get-AsciiRelativePath $chunkBase)
                    )
                    if ($language -eq 'zh' -and $terms.Count -gt 0) { $responseArgs += @('--prompt', ($terms -join '、')) }
                    [System.IO.File]::WriteAllLines($responsePath, $responseArgs, $utf8NoBom)
                    $responseArgument = '@' + (Get-AsciiRelativePath $responsePath)
                    Invoke-LoggedNative -Exe $WhisperCli -Arguments @($responseArgument) -Label "Whisper 辨識中：$($File.Name)（$chunkNumber / $totalChunks）" -WorkingDirectory $ProjectRoot
                    if (-not (Test-Path -LiteralPath ($chunkBase + '.txt'))) { throw "Whisper 分段沒有產生 txt：$chunkNumber / $totalChunks" }
                    if (-not (Test-Path -LiteralPath ($chunkBase + '.srt'))) { throw "Whisper 分段沒有產生 srt：$chunkNumber / $totalChunks" }
                    Set-Content -LiteralPath $chunkMarker -Value $asrCheckpointSignature -Encoding ASCII
                }

                Write-JobStatus $statusPath $jobId $File.FullName 'whisper_chunk_completed' ("已完成分段 $chunkNumber / $totalChunks")
                $partialRawTxt = Join-Path $jobDir 'partial.txt'
                $partialRawSrt = Join-Path $jobDir 'partial.srt'
                Merge-WhisperChunks $Python $chunkDir $chunkNumber $partialRawTxt $partialRawSrt $chunkSeconds "合併目前分段：$chunkNumber / $totalChunks"
                # 轉錄進度是可見進度契約，一律產生；各部分檔的產生條件比照它對應的桌面交付 token。
                if ($desktopOutputs -contains 'txt') {
                    Invoke-LoggedNative -Exe $Python -Arguments @((Join-Path $PSScriptRoot 'normalize_text.py'),$partialRawTxt,$desktopPartialPlainTxt) -Label '更新轉錄中純文字 TXT' -WorkingDirectory $ProjectRoot
                }
                if (($desktopOutputs -contains 'timeline') -or ($desktopOutputs -contains 'srt')) {
                    $partialNormalizedSrt = Join-Path $jobDir 'partial.normalized.srt'
                    Invoke-LoggedNative -Exe $Python -Arguments @((Join-Path $PSScriptRoot 'normalize_text.py'),$partialRawSrt,$partialNormalizedSrt) -Label '更新轉錄中時間碼稿' -WorkingDirectory $ProjectRoot
                    if ($desktopOutputs -contains 'timeline') { Copy-Item -LiteralPath $partialNormalizedSrt -Destination $desktopPartialTimecodeTxt -Force }
                    if ($desktopOutputs -contains 'srt') { Copy-Item -LiteralPath $partialNormalizedSrt -Destination $desktopPartialSrt -Force }
                }
                Write-TranscriptionProgress $desktopProgress $chunkNumber $totalChunks
            }

            Write-JobStatus $statusPath $jobId $File.FullName 'merging_chunks' "合併 $totalChunks 個 Whisper 分段"
            $rawTxt = Join-Path $rawDir ($base + '.txt')
            $rawSrt = Join-Path $rawDir ($base + '.srt')
            Merge-WhisperChunks $Python $chunkDir $totalChunks $rawTxt $rawSrt $chunkSeconds '合併完整 Whisper 分段'

            Write-JobStatus $statusPath $jobId $File.FullName 'normalizing_s2tw' 'OpenCC s2tw 單一繁體出口'
            Invoke-LoggedNative -Exe $Python -Arguments @((Join-Path $PSScriptRoot 'normalize_text.py'),$rawTxt,$finalTxt) -Label '正規化 TXT 為臺灣正體中文' -WorkingDirectory $ProjectRoot
            Invoke-LoggedNative -Exe $Python -Arguments @((Join-Path $PSScriptRoot 'normalize_text.py'),$rawSrt,$finalSrt) -Label '正規化 SRT 為臺灣正體中文' -WorkingDirectory $ProjectRoot
        } else {
            Write-RunLog "逐字稿已存在，保留並檢查校稿：$($File.Name)"
        }

        # 只有「複製到桌面」這一段依 desktop_outputs 過濾；output_final 仍保留完整證據。
        if ($desktopOutputs -contains 'txt') { Copy-GeneratedFile $finalTxt $desktopPlainTxt -ForceCopy:$Force }
        if ($desktopOutputs -contains 'srt') { Copy-GeneratedFile $finalSrt $desktopSrt -ForceCopy:$Force }
        # <base>.txt 一律強制覆寫：這個名字在舊版是無時間碼純文字稿，不覆寫會讓舊檔冒充時間碼稿。
        if ($desktopOutputs -contains 'timeline') { Copy-GeneratedFile $finalSrt $desktopTimecodeTxt -ForceCopy:$true }
        # 部分檔一律無條件清除（含舊命名），避免設定變動或改名後殘留上一次的「轉錄中」產物。
        foreach ($progressArtifact in @($desktopPartialTimecodeTxt,$desktopPartialPlainTxt,$desktopPartialSrt,$legacyDesktopPartialTimelineTxt,$desktopProgress)) {
            if (Test-Path -LiteralPath $progressArtifact) { Remove-Item -LiteralPath $progressArtifact -Force }
        }

        $cleanupWarning = $null
        if (Test-OllamaCleanupEnabled $Config) {
            if ($Force -and (Test-Path -LiteralPath $cleanedTxt)) {
                Remove-Item -LiteralPath $cleanedTxt -Force
            }
            if ($shouldCleanup -and ($Force -or -not (Test-Path -LiteralPath $cleanedTxt))) {
                if (Test-Path -LiteralPath $desktopCleaned) { Remove-Item -LiteralPath $desktopCleaned -Force }
                Write-JobStatus $statusPath $jobId $File.FullName 'qwen_cleanup' '本機 Qwen 校稿；只接受 response 欄位'
                try { Invoke-QwenCleanup $Config $Python $finalTxt $cleanedTxt }
                catch {
                    $cleanupWarning = $_.Exception.Message
                    $script:CleanupUnavailable = $cleanupWarning
                    Write-RunLog "警告：Qwen 校稿未完成，本次啟動暫停後續校稿；重新開啟可重試。未建立假校稿檔：$cleanupWarning"
                }
            }
            if ((Test-Path -LiteralPath $cleanedTxt) -and ($desktopOutputs -contains 'cleaned')) {
                Copy-GeneratedFile $cleanedTxt $desktopCleaned -ForceCopy:$Force
            }
        } elseif ($Force) {
            foreach ($staleCleaned in @($cleanedTxt,$desktopCleaned)) {
                if (Test-Path -LiteralPath $staleCleaned) { Remove-Item -LiteralPath $staleCleaned -Force }
            }
        }

        Write-JobStatus $statusPath $jobId $File.FullName 'quality_report' '產生結構檢查；內容仍需人工驗收'
        $qualityArgs = @((Join-Path $PSScriptRoot 'quality_report.py'),$finalTxt,$finalSrt,'--output',$qualityPath,'--review-output',$reviewPath)
        if (Test-Path -LiteralPath $cleanedTxt) { $qualityArgs += @('--cleaned',$cleanedTxt) }
        Invoke-LoggedNative -Exe $Python -Arguments $qualityArgs -Label '產生結構檢查報告' -WorkingDirectory $ProjectRoot

        $recheckWarning = $null
        $recheckReviewRequired = $false
        if ($needsAsr -and (Get-ConfigValue $Config 'asr_recheck' $false)) {
            foreach ($staleRecheck in @($recheckPath,$recheckJson,$desktopRecheck)) {
                if (Test-Path -LiteralPath $staleRecheck) { Remove-Item -LiteralPath $staleRecheck -Force }
            }
            Write-JobStatus $statusPath $jobId $File.FullName 'asr_rechecking' '自動重查開場、結尾及疑點；原始辨識稿保持不變'
            $recheckDir = Join-Path $jobDir 'recheck'
            New-Item -ItemType Directory -Path $recheckDir -Force | Out-Null
            $recheckConfigPath = Join-Path $recheckDir 'config.json'
            $Config | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $recheckConfigPath -Encoding UTF8
            $recheckArgs = @((Join-Path $PSScriptRoot 'recheck_audio.py'),'--config',$recheckConfigPath,'--wav',$wavPath,
                '--srt',$finalSrt,'--output-dir',$recheckDir,'--chunk-seconds',"$chunkSeconds")
            if ((Test-OllamaCleanupEnabled $Config) -and -not $script:CleanupUnavailable) { $recheckArgs += '--qwen' }
            try {
                Invoke-LoggedNative -Exe $Python -Arguments $recheckArgs -Label '音訊疑點重查與候選比對' -WorkingDirectory $ProjectRoot
                Copy-Item -LiteralPath (Join-Path $recheckDir 'report.json') -Destination $recheckJson -Force
                Copy-Item -LiteralPath (Join-Path $recheckDir 'comparison.txt') -Destination $recheckPath -Force
            } catch {
                $recheckWarning = '音訊重查未完成；原始稿仍保留：' + $_.Exception.Message
                Write-RunLog $recheckWarning
            }
        } elseif ($Force) {
            foreach ($staleRecheck in @($recheckPath,$recheckJson,$desktopRecheck)) {
                if (Test-Path -LiteralPath $staleRecheck) { Remove-Item -LiteralPath $staleRecheck -Force }
            }
        }
        if (Test-Path -LiteralPath $recheckJson) {
            $recheckResult = Get-Content -LiteralPath $recheckJson -Raw -Encoding UTF8 | ConvertFrom-Json
            $recheckReviewRequired = $true
            $recheckWarning = '疑點重查候選已另存；有差異不代表重查版本正確，仍須對照原音。'
            if (@($recheckResult.errors).Count -gt 0) { $recheckWarning += '部分重查或 Qwen 比對未完成。' }
            if (@($recheckResult.pending_regions).Count -gt 0) { $recheckWarning += '其餘疑點已列為尚未重查。' }
            Copy-Item -LiteralPath $recheckPath -Destination $desktopRecheck -Force
            Add-Content -LiteralPath $reviewPath -Value ("`n" + $recheckWarning + "`n比對報告：" + $desktopRecheck) -Encoding UTF8
        } elseif ($recheckWarning) {
            $recheckReviewRequired = $true
            Add-Content -LiteralPath $reviewPath -Value ("`n" + $recheckWarning) -Encoding UTF8
        }

        $contentReviewWarning = $null
        $qualityResult = Get-Content -LiteralPath $qualityPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $contentReviewBlocked = ($qualityResult.content_review_status -eq 'blocked')
        if ($contentReviewBlocked) {
            $contentReviewWarning = "機器審查未通過：偵測到 $($qualityResult.blocking_issue_count) 個阻擋型重複疑點；請查看審查報告並對照原音"
            Write-JobStatus $statusPath $jobId $File.FullName 'content_review_blocked' $contentReviewWarning
        }

        # 安全例外：即使沒有啟用 review，機器審查為 blocked 時仍必須把審查報告放上桌面，不得無聲。
        if (($desktopOutputs -contains 'review') -or $contentReviewBlocked -or $recheckReviewRequired) {
            Copy-GeneratedFile $reviewPath $desktopReview -ForceCopy:$true
            if ($contentReviewBlocked -and ($desktopOutputs -notcontains 'review')) {
                Write-RunLog "機器審查未通過，仍在桌面保留審查報告：$base.審查報告.txt"
            }
        }

        # 清掉本次設定已停用、但桌面還留著的同 base 舊產物；blocked 的審查報告不算停用。
        # 已淘汰的舊命名（<base>.時間軸.txt）一律清除，讓桌面不會新舊混雜。
        $disabledDesktopArtifacts = @($legacyDesktopTimelineTxt)
        if ($desktopOutputs -notcontains 'txt') { $disabledDesktopArtifacts += $desktopPlainTxt }
        if ($desktopOutputs -notcontains 'timeline') { $disabledDesktopArtifacts += $desktopTimecodeTxt }
        if ($desktopOutputs -notcontains 'srt') { $disabledDesktopArtifacts += $desktopSrt }
        if ($desktopOutputs -notcontains 'cleaned') { $disabledDesktopArtifacts += $desktopCleaned }
        if (($desktopOutputs -notcontains 'review') -and -not $contentReviewBlocked -and -not $recheckReviewRequired) { $disabledDesktopArtifacts += $desktopReview }
        foreach ($disabledArtifact in $disabledDesktopArtifacts) {
            if (Test-Path -LiteralPath $disabledArtifact) {
                Remove-Item -LiteralPath $disabledArtifact -Force
                Write-RunLog ('移除本次設定已停用的桌面舊產物：' + [System.IO.Path]::GetFileName($disabledArtifact))
            }
        }

        $warnings = @($cleanupWarning,$contentReviewWarning,$recheckWarning) | Where-Object { $_ }
        if ($warnings.Count -gt 0) {
            Write-JobStatus $statusPath $jobId $File.FullName 'completed_with_warning' ($warnings -join '；')
            Write-RunLog "轉錄完成但需處理警告：$base"
        } else {
            Write-JobStatus $statusPath $jobId $File.FullName 'completed' 'TXT、SRT 與可用的校稿輸出已完成'
            Write-RunLog "已完成輸出：$base.txt / $base.srt"
        }
        Write-CompletedMedia $File $Workspace 'pipeline_completed'
    }
    catch {
        Write-JobStatus $statusPath $jobId $File.FullName 'failed' $_.Exception.Message
        throw
    }
}

try {
    $config = Get-RequiredConfig
    $Workspace = Get-ConfigValue $config 'workspace_dir' $ProjectRoot
    $workspaceFull = [System.IO.Path]::GetFullPath($Workspace)
    $rootFull = [System.IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
    if (-not ($workspaceFull -eq $rootFull -or $workspaceFull.StartsWith($rootFull + '\', [System.StringComparison]::OrdinalIgnoreCase))) {
        throw "workspace_dir 必須位於正式根目錄內：$ProjectRoot"
    }
    $logDir = Join-Path $Workspace 'logs'
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $script:LogPath = Join-Path $logDir ('run_{0}.log' -f (Get-Date -Format 'yyyyMMdd_HHmmss'))

    $desktopInput = Get-ConfigValue $config 'desktop_input_dir' (Join-Path ([Environment]::GetFolderPath('Desktop')) '廣播音檔')
    $desktopOutput = Get-ConfigValue $config 'desktop_output_dir' (Join-Path ([Environment]::GetFolderPath('Desktop')) '廣播文字檔')
    $script:SelectedInputPaths = @(Get-ConfigValue $config 'selected_input_paths' @())
    foreach ($selectedPath in $script:SelectedInputPaths) {
        $inputPrefix = [IO.Path]::GetFullPath($desktopInput).TrimEnd('\') + '\'
        if (-not [IO.Path]::GetFullPath($selectedPath).StartsWith($inputPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "選取的音檔不在音檔資料夾內：$selectedPath"
        }
    }
    $desktopOutputTokens = Get-DesktopOutputTokens $config
    foreach ($dir in @($desktopInput,$desktopOutput,(Join-Path $Workspace 'input_audio'),(Join-Path $Workspace 'work_wav'),(Join-Path $Workspace 'whisper_raw'),(Join-Path $Workspace 'output_final'))) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }

    $ffmpeg = Find-Ffmpeg $config
    $ffprobe = Find-Ffprobe $config $ffmpeg
    $whisperCli = Find-WhisperCli $config
    $python = Get-PythonPath
    $modelPath = Get-ConfigValue $config 'model_path' (Join-Path $ProjectRoot 'models\ggml-large-v3.bin')
    $vadModelPath = Get-ConfigValue $config 'vad_model_path' (Join-Path $ProjectRoot 'models\ggml-silero-v6.2.0.bin')
    $missing = @()
    if (-not $ffmpeg) { $missing += 'FFmpeg' }
    if (-not $ffprobe) { $missing += 'FFprobe' }
    if (-not $whisperCli) { $missing += 'whisper-cli.exe' }
    if (-not $python) { $missing += '專案 .venv Python' }
    if (-not (Test-Path -LiteralPath $modelPath)) { $missing += 'ggml-large-v3.bin 模型檔' }
    if (-not (Test-Path -LiteralPath $vadModelPath)) { $missing += 'ggml-silero-v6.2.0.bin VAD 模型檔' }
    if ($missing.Count -gt 0) { throw ('缺少必要項目：' + ($missing -join '、') + '。請執行 scripts\verify_windows.ps1。') }

    Invoke-LoggedNative -Exe $python -Arguments @('-c',"from opencc import OpenCC; assert OpenCC('s2tw').convert('汉字') == '漢字'") -Label '檢查本機 Python／OpenCC（不需網路）'
    $manifest = Get-Content -LiteralPath (Join-Path $ProjectRoot 'config\model-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($entry in @(@{ Role='speech_to_text'; Path=$modelPath }, @{ Role='voice_activity_detection'; Path=$vadModelPath })) {
        $expected = $manifest.models | Where-Object { $_.role -eq $entry.Role } | Select-Object -First 1
        Write-RunLog ('檢查本機模型完整性：' + [IO.Path]::GetFileName($entry.Path))
        $sha = [Security.Cryptography.SHA256]::Create()
        $stream = [IO.File]::OpenRead($entry.Path)
        try { $actualHash = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-','') }
        finally { $stream.Dispose(); $sha.Dispose() }
        if (-not $expected -or $actualHash -ne $expected.sha256) {
            throw ('模型完整性檢查失敗，請修復後再啟動：' + $entry.Path)
        }
    }
    if (Test-OllamaCleanupEnabled $config) {
        . (Join-Path $PSScriptRoot 'ensure_ollama.ps1')
        Write-RunLog '檢查並自動啟動本機 Ollama；最多等待約 30 秒，不需要網際網路。'
        try {
            $null = Ensure-OllamaReady $config
            Write-RunLog 'Ollama 與指定 Qwen 模型已就緒；首次校稿時載入模型。'
        } catch {
            $script:CleanupUnavailable = $_.Exception.Message
            Write-RunLog ('警告：本次暫停 Qwen 校稿，仍可離線轉錄並產生時間碼稿。原因：' + $script:CleanupUnavailable)
        }
    }

    Write-RunLog '轉錄系統已啟動。'
    Write-RunLog ('監控資料夾：' + $desktopInput)
    Write-RunLog ('輸出資料夾：' + $desktopOutput)
    Write-RunLog ('桌面交付內容（desktop_outputs）：' + $(if ($desktopOutputTokens.Count -gt 0) { $desktopOutputTokens -join '、' } else { '（無）' }))
    Write-RunLog ('檔案大小連續 {0} 分鐘不變才會開始處理。' -f $StableMinutes)
    if (-not $Once) { Write-RunLog '此視窗開著就會自動偵測；關閉視窗即停止。' }

    $state = @{}
    do {
        $files = @(Get-MediaFiles $desktopInput)
        if ($files.Count -eq 0) {
            Write-RunLog $(if ($OnlyName) { "找不到指定檔案：$OnlyName" } else { '請先把音檔或影片放到桌面「廣播音檔」資料夾。' })
            if ($Once) { break }
            Start-Sleep -Seconds $PollSeconds
            continue
        }

        Write-RunLog ('找到 {0} 個音檔/影片。' -f $files.Count)
        $ready = @()
        $now = Get-Date
        foreach ($file in $files) {
            if ($script:ProcessedJobs.ContainsKey((Get-JobId $file))) { continue }
            if (-not $Force -and -not $RetryCleanup -and (Test-CompletedMedia $file $Workspace $desktopOutput)) {
                Write-RunLog ('略過：此音檔內容已完成轉錄；文字檔改名或移動仍保留，不重轉、不補回原檔名：' + $file.Name)
                $script:ProcessedJobs[(Get-JobId $file)] = $true
                continue
            }
            $key = $file.FullName
            if (-not $state.ContainsKey($key)) {
                $stableSince = if ($file.LastWriteTime -lt $now.AddMinutes(-1 * $StableMinutes)) { $file.LastWriteTime } else { $now }
                $state[$key] = [pscustomobject]@{ Length = $file.Length; StableSince = $stableSince }
            } elseif ($state[$key].Length -ne $file.Length) {
                $state[$key] = [pscustomobject]@{ Length = $file.Length; StableSince = $now }
            }
            $stableFor = ($now - $state[$key].StableSince).TotalMinutes
            if ($stableFor -ge $StableMinutes) { $ready += $file }
            else { Write-RunLog ('等待檔案複製完成：{0}（已穩定 {1:N1}/{2} 分鐘）' -f $file.Name, [Math]::Max(0, $stableFor), $StableMinutes) }
        }

        $contentOwners = @{}
        # Seed owners before prioritizing new work, including jobs already handled
        # this Run. Otherwise a newly arrived duplicate would run Whisper again.
        foreach ($existingFile in $files) {
            $existingBase = Get-MediaBase $existingFile
            if ((Test-Path -LiteralPath (Join-Path $Workspace "output_final\$existingBase.txt")) -and
                (Test-Path -LiteralPath (Join-Path $Workspace "output_final\$existingBase.srt"))) {
                $contentOwners[(Get-MediaFingerprint $existingFile)] = $existingBase
            }
        }
        $queue = @()
        # New transcripts take priority over old files awaiting optional proofreading.
        $ready = @($ready | Sort-Object @{Expression={
            $b = Get-MediaBase $_
            [int]((Test-Path -LiteralPath (Join-Path $Workspace "output_final\$b.txt")) -and (Test-Path -LiteralPath (Join-Path $Workspace "output_final\$b.srt")))
        }}, Name)
        foreach ($file in $ready) {
            $fingerprint = Get-MediaFingerprint $file
            $base = Get-MediaBase $file
            $reuseFromBase = ''
            if ($contentOwners.ContainsKey($fingerprint)) { $reuseFromBase = $contentOwners[$fingerprint] }
            else { $contentOwners[$fingerprint] = $base }
            $queue += [pscustomobject]@{ File = $file; Base = $base; ReuseFromBase = $reuseFromBase }
        }

        for ($queueIndex = 0; $queueIndex -lt $queue.Count; $queueIndex++) {
            $queued = $queue[$queueIndex]
            $queuedFinalTxt = Join-Path (Join-Path $Workspace 'output_final') ($queued.Base + '.txt')
            $queuedFinalSrt = Join-Path (Join-Path $Workspace 'output_final') ($queued.Base + '.srt')
            if ($Force -or -not ((Test-Path -LiteralPath $queuedFinalTxt) -and (Test-Path -LiteralPath $queuedFinalSrt))) {
                $queuedProgress = Join-Path $desktopOutput ($queued.Base + '.轉錄進度.txt')
                Write-QueuedTranscriptionProgress $queuedProgress ($queueIndex + 1) $queue.Count $queued.ReuseFromBase
            }
        }

        foreach ($queued in $queue) {
            $file = $queued.File
            try {
                Process-MediaFile $file $config $Workspace $ffmpeg $ffprobe $whisperCli $python -ReuseFromBase $queued.ReuseFromBase
                $script:ProcessedJobs[(Get-JobId $file)] = $true
            }
            catch { $script:HadFailures = $true; Write-RunLog ('錯誤：' + $_.Exception.Message) }
            $state.Remove($file.FullName)
        }
        if ($Once) { break }
        Start-Sleep -Seconds $PollSeconds
    } while ($true)

    Write-RunLog '完成後請到上述「廣播文字檔」輸出資料夾查看結果。'
    if ($script:HadFailures) { exit 1 }
}
catch {
    $msg = '錯誤：' + $_.Exception.Message
    Write-Host $msg -ForegroundColor Red
    Add-Content -LiteralPath $script:LogPath -Value $msg -Encoding UTF8
    exit 1
}
finally {
    if ($script:RunnerMutexAcquired) {
        try { $script:RunnerMutex.ReleaseMutex() } catch {}
    }
    if ($script:RunnerMutex) { $script:RunnerMutex.Dispose() }
}
