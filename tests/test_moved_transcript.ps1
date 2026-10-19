$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $root 'scripts\run_transcribe.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw $errors[0] }
foreach ($name in @('Get-MediaBase','Test-DeliveredTranscript')) {
    $node = $ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name}, $true)
    Invoke-Expression $node.Extent.Text
}
function Write-RunLog { param($Message) }
$case = Join-Path $root ('test_artifacts\moved_' + [guid]::NewGuid().ToString('N'))
$desktopInput = Join-Path $case 'input'
$output = Join-Path $case 'output'
$archive = Join-Path $output 'archive\nested'
New-Item -ItemType Directory -Path $desktopInput,$archive | Out-Null
$filePath = Join-Path $desktopInput 'forum.mp4'
[IO.File]::WriteAllText($filePath, 'not media - detection only')
$file = Get-Item -LiteralPath $filePath
$transcript = Join-Path $archive 'forum.txt'
[IO.File]::WriteAllText($transcript, 'completed transcript')
if (-not (Test-DeliveredTranscript $file $output)) { throw 'Moved transcript was missed' }
if (Test-Path (Join-Path $output 'forum.txt')) { throw 'Unexpected root copy' }
[IO.File]::WriteAllText($transcript, '')
foreach ($suffix in @('cleaned','review','progress','partial')) {
    [IO.File]::WriteAllText((Join-Path $archive "forum.$suffix.txt"), 'not a transcript')
}
if (Test-DeliveredTranscript $file $output) { throw 'Empty or auxiliary transcript accepted' }
[IO.File]::WriteAllText((Join-Path $archive 'forum.srt'), 'completed subtitle')
if (-not (Test-DeliveredTranscript $file $output)) { throw 'Moved SRT was missed' }
$other = Join-Path $desktopInput 'other'
New-Item -ItemType Directory -Path $other | Out-Null
[IO.File]::WriteAllText((Join-Path $other 'forum.mp4'), 'different media')
if (Test-DeliveredTranscript $file $output) { throw 'Ambiguous basename incorrectly skipped' }
[IO.File]::WriteAllText((Join-Path $output 'forum.txt'), 'exact path remains authoritative')
if (-not (Test-DeliveredTranscript $file $output)) { throw 'Exact path regression' }
Write-Output "PASS: moved TXT/SRT, empty/aux exclusion, ambiguous basename, exact path; evidence $case"
