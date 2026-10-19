param(
    [Parameter(Mandatory = $true)][string]$Base,
    [Parameter(Mandatory = $true)][string]$Start,
    [Parameter(Mandatory = $true)][string]$End,
    [string]$Language = 'en',
    [double]$VadThreshold = 0.50,
    [switch]$Apply,
    [switch]$DeleteOnly,
    [string]$ConfigPath
)

# 區段重轉修補工具：只重轉正式 SRT 的某個時間區間，並把結果接回 SRT。
# 本工具刻意不 dot-source run_transcribe.ps1，避免動到產品主線；ASR 參數與正式管線一致，只有 -l 不同。
# 硬邊界：只寫 SRT，寫回前一定備份，正式 TXT 不同步。
#
# 受影響 cue 的定義（兩種模式共用）：與 [Start,End) 有時間交集的所有 cue，
# 也就是 cue.Start < End 且 cue.End > Start；只用「起點落在區間內」會兩端都出事。
#
# 邊界處理（兩種模式刻意不同，改動前請先讀懂差異）：
# - 重轉模式：一定把切音與刪除的邊界「向外對齊」到受影響 cue 的完整起訖
#   [min(受影響 cue 起點), max(受影響 cue 終點)]。不對齊的話，尾端跨越 End 的那句只會有前面
#   零點幾秒被餵給 whisper，整句永久消失；頭端跨越 Start 的那句會被保留卻又被重新辨識一次，
#   產生重複文字。沒有任何受影響 cue 時退回使用者給的 Start/End。
# - -DeleteOnly 模式：刻意「不」向外對齊。刪除就是刪除，不應該把邊界擴大到使用者沒指定的 cue，
#   只刪掉與使用者給的 [Start,End) 真正有交集的那些 cue。

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RunStamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$LogDir = Join-Path $ProjectRoot 'logs'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$script:LogPath = Join-Path $LogDir ('repair_{0}.log' -f $RunStamp)

$MediaExtensions = @('.mp3','.wav','.m4a','.mp4','.aac','.flac','.mov','.mkv','.avi','.wmv','.webm','.ogg','.wma','.ts','.mts','.m2ts','.3gp')
$TxtNotSyncedNotice = '注意：本工具只修 SRT。SRT 是時間碼與內容權威；正式 TXT 沒有時間碼、無法對齊區間，因此不同步，也不會用 SRT 反推 TXT 冒充。'

function Write-RepairLog {
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
    Write-RepairLog $Label
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

# --- 最小設定讀取：config.local.json 缺鍵時逐鍵 fallback 到 config.example.json ---

function Read-JsonFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
}

$LocalConfigPath = if ($ConfigPath) { $ConfigPath } else { Join-Path $ProjectRoot 'config.local.json' }
$ExampleConfigPath = Join-Path $ProjectRoot 'config.example.json'
$script:LocalConfig = Read-JsonFile $LocalConfigPath
$script:ExampleConfig = Read-JsonFile $ExampleConfigPath
if (-not $script:LocalConfig -and -not $script:ExampleConfig) {
    throw "找不到任何設定檔：$LocalConfigPath 與 $ExampleConfigPath 都不存在。"
}

function Get-RepairConfigValue {
    param([string]$Name, $Default)
    foreach ($config in @($script:LocalConfig, $script:ExampleConfig)) {
        if ($null -eq $config) { continue }
        if (($config.PSObject.Properties.Name -contains $Name) -and $null -ne $config.$Name -and "$($config.$Name)" -ne '') {
            return $config.$Name
        }
    }
    return $Default
}

function Get-DesktopOutputTokens {
    $allowed = @('srt','txt','timeline','review','cleaned')
    $raw = $null
    foreach ($config in @($script:LocalConfig, $script:ExampleConfig)) {
        if ($null -eq $config) { continue }
        if (($config.PSObject.Properties.Name -contains 'desktop_outputs') -and $null -ne $config.desktop_outputs) {
            $raw = $config.desktop_outputs
            break
        }
    }
    if ($null -eq $raw) { return @('timeline') }
    if ($raw -is [string] -or $raw -isnot [System.Collections.IEnumerable]) {
        throw "desktop_outputs 必須是字串陣列，例如 [""timeline""]。允許的項目：$($allowed -join '、')"
    }
    $tokens = @()
    foreach ($item in $raw) {
        $token = "$item".Trim().ToLowerInvariant()
        if ($allowed -notcontains $token) { throw "desktop_outputs 含未知項目「$token」。" }
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

function Find-Tool {
    param([string]$ConfigKey, [string]$ExeName, [string]$SearchRoot)
    $configured = Get-RepairConfigValue $ConfigKey $null
    if ($configured -and (Test-Path -LiteralPath $configured)) { return $configured }
    $system = Resolve-Exe $ExeName
    if ($system) { return $system }
    if ($SearchRoot -and (Test-Path -LiteralPath $SearchRoot)) {
        $found = Get-ChildItem -LiteralPath $SearchRoot -Filter ($ExeName + '.exe') -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    return $null
}

# --- ASCII 路徑鐵則：whisper 的 argv 與 response file 內容只能是專案根目錄下的 ASCII 相對路徑 ---

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

# --- 時間碼工具 ---

function ConvertFrom-ClockText {
    param([string]$Value, [string]$Label)
    $match = [regex]::Match($Value.Trim(), '^(?<h>\d{1,3}):(?<m>[0-5]\d):(?<s>[0-5]\d)(?:[.,](?<f>\d{1,3}))?$')
    if (-not $match.Success) {
        throw "$Label 必須是 hh:mm:ss 或 hh:mm:ss.fff 格式：$Value"
    }
    $fraction = $match.Groups['f'].Value
    if ($fraction) { $fraction = $fraction.PadRight(3, '0') } else { $fraction = '000' }
    return ((([int]$match.Groups['h'].Value * 60 + [int]$match.Groups['m'].Value) * 60) + [int]$match.Groups['s'].Value) * 1000 + [int]$fraction
}

function Format-SrtTimestamp {
    param([long]$Milliseconds)
    if ($Milliseconds -lt 0) { $Milliseconds = 0 }
    $hours = [math]::Floor($Milliseconds / 3600000)
    $rest = $Milliseconds % 3600000
    $minutes = [math]::Floor($rest / 60000)
    $rest = $rest % 60000
    $seconds = [math]::Floor($rest / 1000)
    $millis = $rest % 1000
    return ('{0:D2}:{1:D2}:{2:D2},{3:D3}' -f [int]$hours, [int]$minutes, [int]$seconds, [int]$millis)
}

function ConvertFrom-SrtText {
    param([string]$Text)
    $cues = @()
    if (-not $Text) { return $cues }
    $normalized = ($Text -replace "`r`n", "`n").Trim()
    if (-not $normalized) { return $cues }
    $order = 0
    foreach ($block in [regex]::Split($normalized, "`n\s*`n")) {
        $lines = @(($block.Trim()) -split "`n")
        if ($lines.Count -lt 3) { throw "SRT 區塊格式錯誤（少於三行）：$block" }
        $timing = [regex]::Match($lines[1].Trim(), '^(\d{2}):(\d{2}):(\d{2}),(\d{3})\s+-->\s+(\d{2}):(\d{2}):(\d{2}),(\d{3})$')
        if (-not $timing.Success) { throw "SRT 時間碼格式錯誤：$($lines[1])" }
        $startMs = ((([int]$timing.Groups[1].Value * 60 + [int]$timing.Groups[2].Value) * 60) + [int]$timing.Groups[3].Value) * 1000 + [int]$timing.Groups[4].Value
        $endMs = ((([int]$timing.Groups[5].Value * 60 + [int]$timing.Groups[6].Value) * 60) + [int]$timing.Groups[7].Value) * 1000 + [int]$timing.Groups[8].Value
        $body = (($lines[2..($lines.Count - 1)] | ForEach-Object { $_.Trim() } | Where-Object { $_ }) -join ' ')
        if (-not $body) { throw "SRT 字幕為空：$block" }
        $cues += [pscustomobject]@{
            Number  = [int]($lines[0].Trim())
            StartMs = [long]$startMs
            EndMs   = [long]$endMs
            Text    = $body
            Order   = $order
        }
        $order++
    }
    return $cues
}

function Select-IntersectingCues {
    # 受影響 cue = 與 [StartMs,EndMs) 有時間交集者。端點剛好相接（前一條的終點等於區間起點）不算交集。
    param($Cues, [long]$StartMs, [long]$EndMs)
    return @($Cues | Where-Object { $_.StartMs -lt $EndMs -and $_.EndMs -gt $StartMs })
}

function ConvertTo-SrtText {
    # 維持既有版式：cue 從 1 連號、每 cue 單行正文、cue 間一個空白行、檔尾一個換行。
    param($Cues)
    $builder = New-Object System.Text.StringBuilder
    $number = 1
    foreach ($cue in $Cues) {
        [void]$builder.Append($number).Append("`n")
        [void]$builder.Append((Format-SrtTimestamp $cue.StartMs)).Append(' --> ').Append((Format-SrtTimestamp $cue.EndMs)).Append("`n")
        [void]$builder.Append($cue.Text).Append("`n")
        [void]$builder.Append("`n")
        $number++
    }
    $text = $builder.ToString()
    if (-not $text) { return '' }
    return $text.TrimEnd("`n") + "`n"
}

function Write-Utf8NoBom {
    param([string]$Path, [string]$Text)
    New-Item -ItemType Directory -Path (Split-Path -Parent $Path) -Force | Out-Null
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding $false))
}

# --- 主流程 ---

try {
    $startMs = ConvertFrom-ClockText $Start '-Start'
    $endMs = ConvertFrom-ClockText $End '-End'
    if ($endMs -le $startMs) { throw '-End 必須晚於 -Start。' }

    # VAD 門檻只允許調鬆或調嚴，不提供關閉 VAD 的選項（AGENTS.md：VAD 是 required runtime）。
    if ($VadThreshold -lt 0.05 -or $VadThreshold -gt 0.95) {
        throw ("-VadThreshold 必須介於 0.05 與 0.95 之間（預設 0.50）：$VadThreshold")
    }
    $vadThresholdText = ([double]$VadThreshold).ToString('0.##', [System.Globalization.CultureInfo]::InvariantCulture)

    $workspace = Get-RepairConfigValue 'workspace_dir' $ProjectRoot
    $workspaceFull = [System.IO.Path]::GetFullPath($workspace)
    $rootFull = [System.IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
    if (-not ($workspaceFull -eq $rootFull -or $workspaceFull.StartsWith($rootFull + '\', [System.StringComparison]::OrdinalIgnoreCase))) {
        throw "workspace_dir 必須位於正式根目錄內：$ProjectRoot"
    }

    $finalDir = Join-Path $workspace 'output_final'
    $finalSrt = Join-Path $finalDir ($Base + '.srt')
    if (-not (Test-Path -LiteralPath $finalSrt)) { throw "找不到正式 SRT：$finalSrt" }

    # 兩種模式都要用專案 .venv Python（重轉用來合併／正規化，-DeleteOnly 用來重跑品質報告）。
    $python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) { throw '缺少必要項目：專案 .venv Python' }

    $actionText = if ($DeleteOnly) { '只刪除受影響 cue（-DeleteOnly，不跑 FFmpeg／Whisper）' } else { '重轉並接回' }
    Write-RepairLog ('區段重轉修補工具啟動；模式：' + $(if ($Apply) { '實跑（-Apply）' } else { '預覽（dry-run，不寫任何正式檔）' }) + '；動作：' + $actionText)
    if ($DeleteOnly) {
        Write-RepairLog ("目標：$Base　你要求的範圍 $Start – $End")
    } else {
        Write-RepairLog ("目標：$Base　你要求的範圍 $Start – $End　語言 $Language　VAD 門檻 $vadThresholdText")
    }
    Write-RepairLog $TxtNotSyncedNotice

    # 1. 先讀正式 SRT，決定受影響 cue 與實際處理邊界。
    #    受影響 = 與 [Start,End) 有時間交集，不是只有「起點落在區間內」。
    $originalText = [System.IO.File]::ReadAllText($finalSrt)
    $originalCues = @(ConvertFrom-SrtText $originalText)
    $effectiveStartMs = [long]$startMs
    $effectiveEndMs = [long]$endMs

    if ($DeleteOnly) {
        # -DeleteOnly 不向外對齊：刪除就是刪除，不擴大到使用者沒指定的 cue。
        $removed = @(Select-IntersectingCues $originalCues $effectiveStartMs $effectiveEndMs)
    } else {
        # 重轉模式向外對齊到受影響 cue 的完整起訖，讓切音、刪除與插入用同一組邊界。
        for ($guard = 0; $guard -lt 100; $guard++) {
            $candidates = @(Select-IntersectingCues $originalCues $effectiveStartMs $effectiveEndMs)
            if ($candidates.Count -eq 0) { break }
            $candidateStart = [long](($candidates | Measure-Object -Property StartMs -Minimum).Minimum)
            $candidateEnd = [long](($candidates | Measure-Object -Property EndMs -Maximum).Maximum)
            if ($candidateStart -ge $effectiveStartMs -and $candidateEnd -le $effectiveEndMs) { break }
            if ($candidateStart -lt $effectiveStartMs) { $effectiveStartMs = $candidateStart }
            if ($candidateEnd -gt $effectiveEndMs) { $effectiveEndMs = $candidateEnd }
        }
        $removed = @(Select-IntersectingCues $originalCues $effectiveStartMs $effectiveEndMs)
    }
    $rangeMs = $effectiveEndMs - $effectiveStartMs
    if ($rangeMs -le 0) { throw '實際處理範圍長度為 0，無法繼續。' }

    $removedOrders = @{}
    foreach ($cue in $removed) { $removedOrders[$cue.Order] = $true }
    $kept = @($originalCues | Where-Object { -not $removedOrders.ContainsKey($_.Order) })

    Write-RepairLog ("你要求的範圍：{0} – {1}" -f $Start, $End)
    if ($DeleteOnly) {
        Write-RepairLog ("實際處理範圍：{0} – {1}（-DeleteOnly 刻意不向外對齊）" -f (Format-SrtTimestamp $effectiveStartMs), (Format-SrtTimestamp $effectiveEndMs))
    } else {
        Write-RepairLog ("向外對齊後的實際範圍：{0} – {1}（對齊到受影響 cue 的完整起訖）" -f (Format-SrtTimestamp $effectiveStartMs), (Format-SrtTimestamp $effectiveEndMs))
    }
    Write-RepairLog ("受影響（與範圍有時間交集）的 cue：{0} 條" -f $removed.Count)
    if ($DeleteOnly -and $removed.Count -eq 0) {
        throw "-DeleteOnly：$Start – $End 內沒有任何有交集的 cue，沒有東西可刪。"
    }

    $newCues = @()
    if (-not $DeleteOnly) {
        # 2. 來源媒體：先找 input_audio，再找設定的 desktop_input_dir。
        $inputAudioDir = Join-Path $workspace 'input_audio'
        $desktopInputDir = Get-RepairConfigValue 'desktop_input_dir' (Join-Path ([Environment]::GetFolderPath('Desktop')) '廣播音檔')
        $sourceMedia = $null
        foreach ($dir in @($inputAudioDir, $desktopInputDir)) {
            if (-not $dir -or -not (Test-Path -LiteralPath $dir)) { continue }
            $match = Get-ChildItem -LiteralPath $dir -File -ErrorAction SilentlyContinue |
                Where-Object { $MediaExtensions -contains $_.Extension.ToLowerInvariant() } |
                Where-Object { [System.IO.Path]::GetFileNameWithoutExtension($_.Name) -eq $Base } |
                Select-Object -First 1
            if ($match) { $sourceMedia = $match.FullName; break }
        }
        if (-not $sourceMedia) {
            throw "找不到來源媒體「$Base」：已找過 $inputAudioDir 與 $desktopInputDir。"
        }
        Write-RepairLog ('來源媒體：' + $sourceMedia)

        $ffmpeg = Find-Tool 'ffmpeg_path' 'ffmpeg' (Join-Path $ProjectRoot 'tools\ffmpeg')
        $whisperCli = Find-Tool 'whisper_cli_path' 'whisper-cli' (Join-Path $ProjectRoot 'tools\whisper.cpp')
        $modelPath = Get-RepairConfigValue 'model_path' (Join-Path $ProjectRoot 'models\ggml-large-v3.bin')
        $vadModelPath = Get-RepairConfigValue 'vad_model_path' (Join-Path $ProjectRoot 'models\ggml-silero-v6.2.0.bin')
        $missing = @()
        if (-not $ffmpeg) { $missing += 'FFmpeg' }
        if (-not $whisperCli) { $missing += 'whisper-cli.exe' }
        if (-not (Test-Path -LiteralPath $python)) { $missing += '專案 .venv Python' }
        if (-not (Test-Path -LiteralPath $modelPath)) { $missing += 'Whisper 模型檔' }
        if (-not (Test-Path -LiteralPath $vadModelPath)) { $missing += 'Silero VAD 模型檔' }
        if ($missing.Count -gt 0) { throw ('缺少必要項目：' + ($missing -join '、')) }

        # 3. 暫存區固定在 ASCII 路徑底下。
        $repairDir = Join-Path (Join-Path (Join-Path $workspace 'whisper_raw') 'repair') $RunStamp
        New-Item -ItemType Directory -Path $repairDir -Force | Out-Null
        Write-RepairLog ('暫存工作目錄：' + $repairDir)

        # 4. 用「向外對齊後」的邊界切出區間並轉成 16 kHz mono pcm_s16le。
        $rangeWav = Join-Path $repairDir 'range.wav'
        $startSecondsText = ([double]($effectiveStartMs / 1000.0)).ToString('0.###', [System.Globalization.CultureInfo]::InvariantCulture)
        $rangeSecondsText = ([double]($rangeMs / 1000.0)).ToString('0.###', [System.Globalization.CultureInfo]::InvariantCulture)
        Invoke-LoggedNative -Exe $ffmpeg -Arguments @(
            '-hide_banner','-loglevel','warning','-y',
            '-ss',$startSecondsText,'-t',$rangeSecondsText,'-i',$sourceMedia,
            '-vn','-ac','1','-ar','16000','-c:a','pcm_s16le',$rangeWav
        ) -Label ("擷取修補區間音訊（對齊後）：{0} – {1}" -f (Format-SrtTimestamp $effectiveStartMs), (Format-SrtTimestamp $effectiveEndMs))

        # 5. 比照正式管線切成 300 秒子段，逐段辨識後再用既有 merge 腳本合併。
        $chunkSeconds = 300
        $chunkDir = Join-Path $repairDir 'chunks'
        New-Item -ItemType Directory -Path $chunkDir -Force | Out-Null
        $totalChunks = [int][Math]::Ceiling($rangeMs / ($chunkSeconds * 1000.0))
        if ($totalChunks -lt 1) { $totalChunks = 1 }
        Write-RepairLog ("區間長度 {0:N3} 秒，切成 {1} 個 {2} 秒子段。" -f ($rangeMs / 1000.0), $totalChunks, $chunkSeconds)

        $signature = 'repair-v1|model={0}|language={1}|vad=true|vt={2}|mc=0|tpi=0.20' -f ([System.IO.Path]::GetFileName($modelPath)), $Language, $vadThresholdText
        $utf8NoBom = New-Object System.Text.UTF8Encoding $false
        for ($chunkIndex = 0; $chunkIndex -lt $totalChunks; $chunkIndex++) {
            $chunkNumber = $chunkIndex + 1
            $chunkBase = Join-Path $chunkDir ('chunk_{0:D5}' -f $chunkIndex)
            $chunkWav = $chunkBase + '.wav'
            $offsetMs = [int64]$chunkIndex * [int64]$chunkSeconds * 1000
            $remainingMs = [int64]$rangeMs - $offsetMs
            $durationMs = [Math]::Min(([int64]$chunkSeconds * 1000), $remainingMs)
            $offsetText = ([double]($offsetMs / 1000.0)).ToString('0.###', [System.Globalization.CultureInfo]::InvariantCulture)
            $durationText = ([double]($durationMs / 1000.0)).ToString('0.###', [System.Globalization.CultureInfo]::InvariantCulture)
            Invoke-LoggedNative -Exe $ffmpeg -Arguments @(
                '-hide_banner','-loglevel','warning','-y',
                '-ss',$offsetText,'-t',$durationText,'-i',$rangeWav,'-c:a','copy',$chunkWav
            ) -Label "建立修補子段：$chunkNumber / $totalChunks"

            $responsePath = Join-Path $chunkDir ('args_{0:D5}.rsp' -f $chunkIndex)
            $responseArgs = @(
                '-m', (Get-AsciiRelativePath $modelPath),
                '-f', (Get-AsciiRelativePath $chunkWav),
                '-l', $Language,
                '-mc', '0', '-tpi', '0.20',
                '--vad', '-vm', (Get-AsciiRelativePath $vadModelPath), '-vt', $vadThresholdText,
                '-otxt', '-osrt', '-of', (Get-AsciiRelativePath $chunkBase)
            )
            [System.IO.File]::WriteAllLines($responsePath, $responseArgs, $utf8NoBom)
            $responseArgument = '@' + (Get-AsciiRelativePath $responsePath)
            Invoke-LoggedNative -Exe $whisperCli -Arguments @($responseArgument) -Label "Whisper 重轉修補子段：$chunkNumber / $totalChunks" -WorkingDirectory $ProjectRoot
            if (-not (Test-Path -LiteralPath ($chunkBase + '.txt'))) { throw "修補子段沒有產生 txt：$chunkNumber / $totalChunks" }
            if (-not (Test-Path -LiteralPath ($chunkBase + '.srt'))) { throw "修補子段沒有產生 srt：$chunkNumber / $totalChunks" }
            Set-Content -LiteralPath ($chunkBase + '.complete') -Value $signature -Encoding ASCII
        }

        # 6. 沿用既有合併腳本，再過 OpenCC s2tw 單一出口。
        $mergedTxt = Join-Path $repairDir 'merged.txt'
        $mergedSrt = Join-Path $repairDir 'merged.srt'
        Invoke-LoggedNative -Exe $python -Arguments @(
            (Join-Path $PSScriptRoot 'merge_whisper_chunks.py'), $chunkDir, "$totalChunks", $mergedTxt, $mergedSrt, "$chunkSeconds"
        ) -Label '合併修補子段' -WorkingDirectory $ProjectRoot

        $normalizedSrt = Join-Path $repairDir 'normalized.srt'
        Invoke-LoggedNative -Exe $python -Arguments @(
            (Join-Path $PSScriptRoot 'normalize_text.py'), $mergedSrt, $normalizedSrt
        ) -Label '修補區段正規化為臺灣正體中文' -WorkingDirectory $ProjectRoot

        # 7. 片段 SRT 時間碼整體加上「對齊後的實際起點」偏移，不是使用者給的 Start。
        $segmentCues = @(ConvertFrom-SrtText ([System.IO.File]::ReadAllText($normalizedSrt)))
        if ($segmentCues.Count -eq 0) { throw '修補區段沒有產生任何 cue；不進行接回，請確認區間或語言設定。' }
        $order = 0
        foreach ($cue in $segmentCues) {
            $newCues += [pscustomobject]@{
                StartMs = [long]($cue.StartMs + $effectiveStartMs)
                EndMs   = [long]($cue.EndMs + $effectiveStartMs)
                Text    = $cue.Text
                Order   = 1000000 + $order
            }
            $order++
        }
    }

    # 8. 接回：刪除受影響 cue、插入新 cue（-DeleteOnly 時沒有新 cue），依時間排序後全域重新連號。
    $combined = @($kept + $newCues) | Sort-Object StartMs, EndMs, Order
    if ($combined.Count -eq 0) { throw '處理後的 SRT 會變成空檔；不寫回。' }
    $mergedText = ConvertTo-SrtText $combined

    $backupDir = Join-Path $finalDir 'repair_backup'
    $backupPath = Join-Path $backupDir ('{0}.{1}.srt' -f $Base, $RunStamp)

    $removedRange = if ($removed.Count -gt 0) { '{0}–{1}' -f $removed[0].Number, $removed[-1].Number } else { '（無）' }
    Write-RepairLog '----- 修補預覽 -----'
    Write-RepairLog ("你要求的範圍：{0} – {1}" -f $Start, $End)
    Write-RepairLog ("實際處理範圍：{0} – {1}{2}" -f (Format-SrtTimestamp $effectiveStartMs), (Format-SrtTimestamp $effectiveEndMs), $(if ($DeleteOnly) { '（-DeleteOnly 不向外對齊）' } else { '（已向外對齊到受影響 cue 的完整起訖）' }))
    Write-RepairLog ("原始 SRT cue 總數：{0}" -f $originalCues.Count)
    Write-RepairLog ("將被刪除的 cue 編號範圍：{0}，共 {1} 條" -f $removedRange, $removed.Count)
    Write-RepairLog ("新產生的 cue 條數：{0}" -f $newCues.Count)
    Write-RepairLog ("修補後 SRT cue 總數：{0}" -f $combined.Count)
    Write-RepairLog ("備份路徑：{0}" -f $backupPath)
    $removedPreviewLimit = if ($DeleteOnly) { $removed.Count } else { 10 }
    if ($removed.Count -gt 0) {
        Write-RepairLog ('將被刪除的 cue（最多顯示 {0} 條）：' -f $removedPreviewLimit)
        $removedPreview = if ($removed.Count -gt $removedPreviewLimit) { $removed[0..($removedPreviewLimit - 1)] } else { $removed }
        foreach ($cue in $removedPreview) {
            Write-RepairLog ('  #{0}  {1} --> {2}  {3}' -f $cue.Number, (Format-SrtTimestamp $cue.StartMs), (Format-SrtTimestamp $cue.EndMs), $cue.Text)
        }
    }
    if (-not $DeleteOnly) {
        Write-RepairLog '新區段前 10 條 cue：'
        $preview = if ($newCues.Count -gt 10) { $newCues[0..9] } else { $newCues }
        foreach ($cue in $preview) {
            Write-RepairLog ('  {0} --> {1}  {2}' -f (Format-SrtTimestamp $cue.StartMs), (Format-SrtTimestamp $cue.EndMs), $cue.Text)
        }
    }
    Write-RepairLog '--------------------'

    if (-not $Apply) {
        Write-RepairLog 'dry-run 結束：沒有寫入 output_final、沒有寫入桌面、沒有建立備份。確認無誤後請加上 -Apply 重跑。'
        Write-RepairLog $TxtNotSyncedNotice
        exit 0
    }

    # 9. 先備份，再寫回。
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    Copy-Item -LiteralPath $finalSrt -Destination $backupPath -Force
    Write-RepairLog ('已備份原始 SRT：' + $backupPath)

    Write-Utf8NoBom $finalSrt $mergedText
    Write-RepairLog ('已寫回正式 SRT：' + $finalSrt)

    # 10. 連號與時序自我檢查。
    $verifyCues = @(ConvertFrom-SrtText ([System.IO.File]::ReadAllText($finalSrt)))
    $numberingOk = $true
    $orderingOk = $true
    for ($index = 0; $index -lt $verifyCues.Count; $index++) {
        if ($verifyCues[$index].Number -ne ($index + 1)) { $numberingOk = $false }
        if ($index -gt 0 -and $verifyCues[$index].StartMs -lt $verifyCues[$index - 1].StartMs) { $orderingOk = $false }
    }
    Write-RepairLog ("寫回後 cue 總數：{0}；連號檢查：{1}；時序檢查：{2}" -f $verifyCues.Count, $(if ($numberingOk) { '通過' } else { '失敗' }), $(if ($orderingOk) { '通過' } else { '失敗' }))
    if (-not $numberingOk -or -not $orderingOk) { throw '寫回後的 SRT 未通過連號或時序檢查；請用備份還原。' }

    # 11. 重跑品質報告，更新 quality.json 與審查報告。
    $finalTxt = Join-Path $finalDir ($Base + '.txt')
    $qualityPath = Join-Path $finalDir ($Base + '.quality.json')
    $reviewPath = Join-Path $finalDir ($Base + '.審查報告.txt')
    $contentReviewBlocked = $false
    if (Test-Path -LiteralPath $finalTxt) {
        $qualityArgs = @((Join-Path $PSScriptRoot 'quality_report.py'), $finalTxt, $finalSrt, '--output', $qualityPath, '--review-output', $reviewPath)
        $cleanedTxt = Join-Path $finalDir ($Base + '.cleaned.txt')
        if (Test-Path -LiteralPath $cleanedTxt) { $qualityArgs += @('--cleaned', $cleanedTxt) }
        Invoke-LoggedNative -Exe $python -Arguments $qualityArgs -Label '重新產生結構檢查與審查報告' -WorkingDirectory $ProjectRoot
        $qualityResult = Get-Content -LiteralPath $qualityPath -Raw -Encoding UTF8 | ConvertFrom-Json
        Write-RepairLog ("機器審查狀態：{0}（阻擋 {1}、警告 {2}）" -f $qualityResult.content_review_status, $qualityResult.blocking_issue_count, $qualityResult.warning_issue_count)
        $contentReviewBlocked = ($qualityResult.content_review_status -eq 'blocked')
    } else {
        Write-RepairLog "警告：找不到正式 TXT（$finalTxt），略過品質報告。"
    }

    # 12. 依 desktop_outputs 把新 SRT 複製到桌面；blocked 時審查報告一律不得無聲。
    #     桌面命名：timeline -> <Base>.txt（正式 SRT 的逐位元副本）、srt -> <Base>.srt。
    $desktopOut = Get-RepairConfigValue 'desktop_output_dir' (Join-Path ([Environment]::GetFolderPath('Desktop')) '廣播文字檔')
    $desktopOutputs = Get-DesktopOutputTokens
    $copiedToDesktop = $false
    if ($desktopOutputs -contains 'timeline') {
        New-Item -ItemType Directory -Path $desktopOut -Force | Out-Null
        Copy-Item -LiteralPath $finalSrt -Destination (Join-Path $desktopOut ($Base + '.txt')) -Force
        Write-RepairLog ('已更新桌面含時間碼 TXT（與 SRT 逐位元一致）：' + (Join-Path $desktopOut ($Base + '.txt')))
        $copiedToDesktop = $true
    }
    if ($desktopOutputs -contains 'srt') {
        New-Item -ItemType Directory -Path $desktopOut -Force | Out-Null
        Copy-Item -LiteralPath $finalSrt -Destination (Join-Path $desktopOut ($Base + '.srt')) -Force
        Write-RepairLog ('已更新桌面 SRT：' + (Join-Path $desktopOut ($Base + '.srt')))
        $copiedToDesktop = $true
    }
    # 舊命名殘留：修補後的時間碼稿已改名，舊檔留著會冒充新結果。
    $legacyDesktopTimeline = Join-Path $desktopOut ($Base + '.時間軸.txt')
    if (Test-Path -LiteralPath $legacyDesktopTimeline) {
        Remove-Item -LiteralPath $legacyDesktopTimeline -Force
        Write-RepairLog ('已移除舊命名的桌面殘留：' + $legacyDesktopTimeline)
    }
    if (-not $copiedToDesktop) {
        Write-RepairLog 'desktop_outputs 未包含 timeline 或 srt，桌面不複製時間碼稿。'
    }
    if ((Test-Path -LiteralPath $reviewPath) -and (($desktopOutputs -contains 'review') -or $contentReviewBlocked)) {
        New-Item -ItemType Directory -Path $desktopOut -Force | Out-Null
        Copy-Item -LiteralPath $reviewPath -Destination (Join-Path $desktopOut ($Base + '.審查報告.txt')) -Force
        Write-RepairLog '機器審查為 blocked 或已啟用 review，審查報告保留在桌面。'
    }

    Write-RepairLog $TxtNotSyncedNotice
    Write-RepairLog '區段修補完成。'
}
catch {
    $message = '錯誤：' + $_.Exception.Message
    Write-Host $message -ForegroundColor Red
    Add-Content -LiteralPath $script:LogPath -Value $message -Encoding UTF8
    exit 1
}
