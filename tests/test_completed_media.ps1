$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $root 'scripts\run_transcribe.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw $errors[0] }
foreach ($name in @('Get-MediaBase','Get-JobId','Get-MediaFingerprint','Test-DeliveredTranscript','Write-CompletedMedia','Test-CompletedMedia','Backup-TranscriptsForRetranscription')) {
    $node = $ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name}, $true)
    Invoke-Expression $node.Extent.Text
}
function Write-RunLog { param($Message) }
$script:MediaHashCache = @{}
$case = Join-Path $root ('test_artifacts\completed_' + [guid]::NewGuid().ToString('N'))
$desktopInput = Join-Path $case 'input'
$output = Join-Path $case 'output'
$workspace = Join-Path $case 'workspace'
$archive = Join-Path $output 'archive'
$final = Join-Path $workspace 'output_final'
New-Item -ItemType Directory -Path $desktopInput,$archive,$final | Out-Null
$media = Join-Path $desktopInput 'forum.mp3'
[IO.File]::WriteAllText($media, 'source one')
$file = Get-Item -LiteralPath $media
$script:BackupStamp = 'recheck-backup-test'
foreach ($suffix in @('.重查比對.txt','.recheck.json')) {
    [IO.File]::WriteAllText((Join-Path $final ('forum' + $suffix)), 'previous recheck evidence')
}
Backup-TranscriptsForRetranscription $file $workspace $output
foreach ($suffix in @('.重查比對.txt','.recheck.json')) {
    $saved = Join-Path $workspace ('work\retranscribe_backups\recheck-backup-test\output_final\forum' + $suffix)
    if (-not (Test-Path $saved) -or [IO.File]::ReadAllText($saved) -ne 'previous recheck evidence') { throw 'Force lost previous recheck evidence' }
}
if (Test-CompletedMedia $file $workspace $output) { throw 'New media skipped' }
[IO.File]::WriteAllText((Join-Path $output 'forum.txt'), 'completed transcript')
if (-not (Test-CompletedMedia $file $workspace $output)) { throw 'Existing delivered transcript missed' }
Move-Item -LiteralPath (Join-Path $output 'forum.txt') -Destination (Join-Path $archive 'renamed.txt')
[IO.File]::WriteAllText((Join-Path $archive 'renamed.txt'), 'human edited transcript')
$script:MediaHashCache = @{} # emulate a fresh process
if (-not (Test-CompletedMedia (Get-Item $media) $workspace $output)) { throw 'Renamed/edited transcript missed after restart' }
Move-Item -LiteralPath $media -Destination (Join-Path $desktopInput 'renamed_audio.mp3')
$media = Join-Path $desktopInput 'renamed_audio.mp3'
if (-not (Test-CompletedMedia (Get-Item $media) $workspace $output)) { throw 'Renamed audio missed' }
Remove-Item -LiteralPath (Join-Path $archive 'renamed.txt')
if (-not (Test-CompletedMedia (Get-Item $media) $workspace $output)) { throw 'Deleting transcript erased completion' }
$duplicate = Join-Path $desktopInput 'new_copy.mp3'
Copy-Item -LiteralPath $media -Destination $duplicate
if (Test-CompletedMedia (Get-Item $duplicate) $workspace $output) { throw 'New copy must retain its own delivery contract' }
Write-CompletedMedia (Get-Item $duplicate) $workspace 'pipeline_completed'
if (-not (Test-CompletedMedia (Get-Item $media) $workspace $output)) { throw 'Duplicate registration lost original completion' }
[IO.File]::WriteAllText($media, 'different source content')
$blocked = $false
try { $null = Test-CompletedMedia (Get-Item $media) $workspace $output }
catch { if ($_.Exception.Message -notmatch '-Force') { throw }; $blocked = $true }
if (-not $blocked) { throw 'Replaced same-path source borrowed old completion' }
Move-Item -LiteralPath $media -Destination (Join-Path $desktopInput 'different.mp3')
if (Test-CompletedMedia (Get-Item (Join-Path $desktopInput 'different.mp3')) $workspace $output) { throw 'Different media incorrectly skipped' }

# Backfill a real legacy completion; partial/failed jobs and mismatched backups must not qualify.
$legacy = Join-Path $desktopInput 'legacy.mp3'
[IO.File]::WriteAllText($legacy, 'legacy source')
$legacyFile = Get-Item $legacy
$statusDir = Join-Path $workspace 'logs\status'
$backupDir = Join-Path $workspace 'input_audio'
New-Item -ItemType Directory -Path $statusDir,$backupDir | Out-Null
$status = Join-Path $statusDir ((Get-JobId $legacyFile) + '.json')
[IO.File]::WriteAllText((Join-Path $final 'legacy.txt'), 'full text')
[IO.File]::WriteAllText((Join-Path $final 'legacy.srt'), "1`n00:00:00,000 --> 00:00:02,000`nfull text`n")
[IO.File]::WriteAllText((Join-Path $backupDir 'legacy.mp3'), 'legacy source')
foreach ($state in @('queued','whisper_transcribing','failed','interrupted')) {
    @{current_status=$state} | ConvertTo-Json | Set-Content $status -Encoding UTF8
    if (Test-CompletedMedia $legacyFile $workspace $output) { throw "Incomplete job accepted: $state" }
}
@{current_status='completed_with_warning'; status_history=@()} | ConvertTo-Json | Set-Content $status -Encoding UTF8
[IO.File]::WriteAllText((Join-Path $backupDir 'legacy.mp3'), 'wrong source')
if (Test-CompletedMedia $legacyFile $workspace $output) { throw 'Mismatched backup accepted' }
[IO.File]::WriteAllText((Join-Path $backupDir 'legacy.mp3'), 'legacy source')
if (-not (Test-CompletedMedia $legacyFile $workspace $output)) { throw 'Verified legacy completion missed' }
if (Test-Path (Join-Path $output 'legacy.txt')) { throw 'Legacy backfill recreated original output' }

# Exercise the real desktop BAT and runner, not only extracted helpers.
$config = Get-Content (Join-Path $root 'config.local.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$config.workspace_dir = $workspace
$config.desktop_input_dir = $desktopInput
$config.desktop_output_dir = $output
$config.use_ollama_cleanup = 'false'
$configPath = Join-Path $case 'config.json'
$config | ConvertTo-Json -Depth 5 | Set-Content $configPath -Encoding UTF8
$desktopBat = Join-Path ([Environment]::GetFolderPath('Desktop')) '開始轉錄.bat'
$previous = $env:BROADCAST_TRANSCRIBE_NO_PAUSE
$env:BROADCAST_TRANSCRIBE_NO_PAUSE = '1'
try {
    & $desktopBat -ConfigPath $configPath -Once -StableMinutes 0 -OnlyName 'legacy.mp3'
    if ($LASTEXITCODE -ne 0) { throw 'Desktop BAT completion gate failed' }
    if (Test-Path (Join-Path $output 'legacy.txt')) { throw 'Desktop BAT recreated original output' }
    $log = Get-ChildItem (Join-Path $workspace 'logs') -Filter 'run_*.log' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ((Get-Content $log.FullName -Raw -Encoding UTF8) -notmatch '此音檔內容已完成轉錄') { throw 'Real runner did not use completion gate' }
    # RetryCleanup explicitly bypasses the gate and restores delivery from full outputs.
    & $desktopBat -ConfigPath $configPath -Once -StableMinutes 0 -OnlyName 'legacy.mp3' -RetryCleanup
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $output 'legacy.txt'))) { throw 'Explicit redelivery regression' }
    Move-Item (Join-Path $output 'legacy.txt') (Join-Path $archive 'edited_legacy.txt')
    & $desktopBat -ConfigPath $configPath -Once -StableMinutes 0 -OnlyName 'legacy.mp3'
    if ($LASTEXITCODE -ne 0 -or (Test-Path (Join-Path $output 'legacy.txt'))) { throw 'Pipeline completion did not survive rename/restart' }
} finally { $env:BROADCAST_TRANSCRIBE_NO_PAUSE = $previous }
Write-Output "PASS: fingerprint persistence, rename/edit/move/delete, different source, guarded legacy backfill, real desktop BAT skip and explicit retry; evidence $case"
