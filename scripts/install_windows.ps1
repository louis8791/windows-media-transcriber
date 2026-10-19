$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ProjectRoot = 'E:\media-transcriber'
$LogDir = Join-Path $ProjectRoot 'logs'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$ReportPath = Join-Path $LogDir 'install_report.txt'
$ConfigPath = Join-Path $ProjectRoot 'config.local.json'
$OllamaModels = Join-Path $ProjectRoot 'models\ollama'
$ToolsDir = Join-Path $ProjectRoot 'tools'
New-Item -ItemType Directory -Path $OllamaModels,$ToolsDir -Force | Out-Null

function Report {
    param([string]$Message)
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Write-Host $line
    Add-Content -LiteralPath $ReportPath -Value $line -Encoding UTF8
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable('Path','Machine')
    $user = [Environment]::GetEnvironmentVariable('Path','User')
    $env:Path = ($machine, $user, $env:Path) -join ';'
}

function Has-Command { param([string]$Name) [bool](Get-Command $Name -ErrorAction SilentlyContinue) }
function Get-CommandPath { param([string]$Name) $cmd = Get-Command $Name -ErrorAction SilentlyContinue; if ($cmd) { $cmd.Source } else { $null } }

function Invoke-External {
    param([string]$Exe, [string[]]$Arguments, [string]$Label)
    Report $Label
    & $Exe @Arguments 2>&1 | ForEach-Object { Report $_.ToString() }
    return ($LASTEXITCODE -eq 0)
}

function Join-ArgumentList {
    param([string[]]$Arguments)
    ($Arguments | ForEach-Object {
        if ($_ -match '[\s"]') { '"' + ($_.Replace('"','\"')) + '"' } else { $_ }
    }) -join ' '
}

function Install-WingetPackage {
    param([string]$Id, [string]$Name, [string[]]$ExtraArgs = @(), [int]$TimeoutSeconds = 1800)
    if (-not (Has-Command 'winget')) { Report "FAIL winget 不存在，無法安裝 $Name"; return $false }
    Report "安裝 $Name：$Id"
    $stdout = Join-Path $LogDir ("winget_{0}_out.log" -f ($Id -replace '[^A-Za-z0-9]+','_'))
    $stderr = Join-Path $LogDir ("winget_{0}_err.log" -f ($Id -replace '[^A-Za-z0-9]+','_'))
    $args = @('install','--id',$Id,'--exact','--accept-package-agreements','--accept-source-agreements','--disable-interactivity','--silent') + $ExtraArgs
    $argLine = Join-ArgumentList -Arguments $args
    $p = Start-Process -FilePath (Get-CommandPath 'winget') -ArgumentList $argLine -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    $exited = $p.WaitForExit($TimeoutSeconds * 1000)
    if (-not $exited) {
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        Report "WARN $Name 安裝逾時，已停止 winget。"
        if (Test-Path -LiteralPath $stdout) { Get-Content -LiteralPath $stdout -Tail 20 -Encoding UTF8 | ForEach-Object { Report $_.ToString() } }
        if (Test-Path -LiteralPath $stderr) { Get-Content -LiteralPath $stderr -Tail 20 -Encoding UTF8 | ForEach-Object { Report $_.ToString() } }
        return $false
    }
    $p.Refresh()
    if (Test-Path -LiteralPath $stdout) { Get-Content -LiteralPath $stdout -Tail 40 -Encoding UTF8 | ForEach-Object { Report $_.ToString() } }
    if (Test-Path -LiteralPath $stderr) { Get-Content -LiteralPath $stderr -Tail 40 -Encoding UTF8 | ForEach-Object { Report $_.ToString() } }
    Refresh-Path
    return ($p.ExitCode -eq 0)
}
function Get-PythonForVenv {
    & py -3.12 --version 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { return @('py','-3.12') }
    & py -3.11 --version 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { return @('py','-3.11') }
    return $null
}

function Find-LocalExe {
    param([string]$Root, [string]$Name)
    if (Test-Path -LiteralPath $Root) {
        $found = Get-ChildItem -LiteralPath $Root -Filter $Name -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    return $null
}

function Get-FfmpegPath {
    $system = Get-CommandPath 'ffmpeg'
    if ($system) { return $system }
    return Find-LocalExe -Root (Join-Path $ToolsDir 'ffmpeg') -Name 'ffmpeg.exe'
}

function Get-CmakePath {
    $system = Get-CommandPath 'cmake'
    if ($system) { return $system }
    $local = Join-Path $ProjectRoot '.venv\Scripts\cmake.exe'
    if (Test-Path -LiteralPath $local) { return $local }
    return $null
}

function Find-WhisperCli {
    $candidates = @(
        (Join-Path $ProjectRoot 'tools\whisper.cpp\build-vs\bin\Release\whisper-cli.exe'),
        (Join-Path $ProjectRoot 'tools\whisper.cpp\build\bin\Release\whisper-cli.exe'),
        (Join-Path $ProjectRoot 'tools\whisper.cpp\build\bin\whisper-cli.exe')
    )
    foreach ($candidate in $candidates) { if (Test-Path -LiteralPath $candidate) { return $candidate } }
    return Find-LocalExe -Root (Join-Path $ProjectRoot 'tools\whisper.cpp\build') -Name 'whisper-cli.exe'
}

function Update-ConfigValue {
    param([string]$Name, [string]$Value)
    if (-not (Test-Path -LiteralPath $ConfigPath)) { return }
    $config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not ($config.PSObject.Properties.Name -contains $Name)) { $config | Add-Member -NotePropertyName $Name -NotePropertyValue $Value }
    else { $config.$Name = $Value }
    $config | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $ConfigPath -Encoding UTF8
}

'' | Set-Content -LiteralPath $ReportPath -Encoding UTF8
Report '開始安裝/檢查。'
Refresh-Path

$pySpec = Get-PythonForVenv
if (-not $pySpec) {
    Install-WingetPackage -Id 'Python.Python.3.12' -Name 'Python 3.12' -TimeoutSeconds 1800 | Out-Null
    $pySpec = Get-PythonForVenv
}
if ($pySpec) { Report ('Python OK：' + (& $pySpec[0] $pySpec[1] --version 2>$null)) } else { Report 'FAIL 找不到 Python 3.11/3.12' }

if (Has-Command 'git') { Report ('Git OK：' + (& git --version)) } else { Install-WingetPackage -Id 'Git.Git' -Name 'Git' -TimeoutSeconds 1800 | Out-Null }
Refresh-Path

if ($pySpec) {
    $venv = Join-Path $ProjectRoot '.venv'
    if (-not (Test-Path -LiteralPath $venv)) {
        Report "建立 venv：$venv"
        & $pySpec[0] $pySpec[1] -m venv $venv 2>&1 | ForEach-Object { Report $_.ToString() }
    } else { Report "venv 已存在：$venv" }
    $venvPython = Join-Path $venv 'Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython) {
        Report '更新 venv 基本套件、文字正規化依賴與本機 CMake。'
        & $venvPython -m pip install --upgrade pip setuptools wheel cmake 2>&1 | ForEach-Object { Report $_.ToString() }
        & $venvPython -m pip install -r (Join-Path $ProjectRoot 'requirements.txt') 2>&1 | ForEach-Object { Report $_.ToString() }
    }
}

$ffmpeg = Get-FfmpegPath
if ($ffmpeg) {
    Report "FFmpeg OK：$ffmpeg"
} else {
    $ffmpegRoot = Join-Path $ToolsDir 'ffmpeg'
    New-Item -ItemType Directory -Path $ffmpegRoot -Force | Out-Null
    $zip = Join-Path $ffmpegRoot 'ffmpeg.zip'
    $url = 'https://github.com/GyanD/codexffmpeg/releases/download/8.1.1/ffmpeg-8.1.1-full_build.zip'
    Report "下載 portable FFmpeg 到 $ffmpegRoot"
    Invoke-External -Exe 'curl.exe' -Arguments @('-L','--fail','--output',$zip,$url) -Label '下載 FFmpeg zip' | Out-Null
    if (Test-Path -LiteralPath $zip) {
        Report '解壓 FFmpeg zip。'
        Expand-Archive -LiteralPath $zip -DestinationPath $ffmpegRoot -Force
    }
    $ffmpeg = Get-FfmpegPath
    if ($ffmpeg) { Report "FFmpeg OK：$ffmpeg"; Update-ConfigValue -Name 'ffmpeg_path' -Value $ffmpeg } else { Report 'FAIL 找不到 FFmpeg' }
}

$cmake = Get-CmakePath
if ($cmake) { Report "CMake OK：$cmake"; Update-ConfigValue -Name 'cmake_path' -Value $cmake } else { Report 'FAIL 找不到 CMake' }

$whisperDir = Join-Path $ProjectRoot 'tools\whisper.cpp'
if (-not (Test-Path -LiteralPath $whisperDir)) {
    if (Has-Command 'git') {
        Report "clone whisper.cpp 到 $whisperDir"
        & git clone https://github.com/ggerganov/whisper.cpp.git $whisperDir 2>&1 | ForEach-Object { Report $_.ToString() }
    } else { Report 'FAIL Git 不存在，無法 clone whisper.cpp' }
} else { Report "whisper.cpp 已存在：$whisperDir" }

$whisperCli = Find-WhisperCli
if (-not $whisperCli -and (Test-Path -LiteralPath $whisperDir) -and $cmake) {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    $vcPath = $null
    if (Test-Path -LiteralPath $vswhere) {
        $vcPath = & $vswhere -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null | Select-Object -First 1
    }
    if (-not $vcPath) {
        Report '未找到 C++ Build Tools，準備安裝 Visual Studio Build Tools。'
        Install-WingetPackage -Id 'Microsoft.VisualStudio.2022.BuildTools' -Name 'Visual Studio 2022 Build Tools' -ExtraArgs @('--override','--wait --quiet --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended') -TimeoutSeconds 5400 | Out-Null
        Refresh-Path
        if (Test-Path -LiteralPath $vswhere) {
            $vcPath = & $vswhere -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null | Select-Object -First 1
        }
    } else { Report "C++ Build Tools OK：$vcPath" }

    if ($vcPath) {
        Report "C++ Build Tools OK：$vcPath"
        $buildDir = Join-Path $whisperDir 'build'
        if (Test-Path -LiteralPath $buildDir) {
            $resolvedBuild = (Resolve-Path -LiteralPath $buildDir).Path
            $resolvedWhisper = (Resolve-Path -LiteralPath $whisperDir).Path
            if ($resolvedBuild.StartsWith($resolvedWhisper, [System.StringComparison]::OrdinalIgnoreCase)) {
                Report "清理先前失敗的 CMake build 資料夾：$buildDir"
                Remove-Item -LiteralPath $buildDir -Recurse -Force
            }
        }
        Report 'CMake 設定 whisper.cpp。'
        & $cmake -S $whisperDir -B $buildDir -G 'Visual Studio 17 2022' -A x64 -DCMAKE_BUILD_TYPE=Release 2>&1 | ForEach-Object { Report $_.ToString() }
        if ($LASTEXITCODE -eq 0) {
            Report 'CMake 建置 whisper-cli。'
            & $cmake --build $buildDir --config Release --target whisper-cli --parallel 2>&1 | ForEach-Object { Report $_.ToString() }
        }
    } else {
        Report 'FAIL C++ Build Tools 不存在，無法建置 whisper-cli.exe'
    }
    $whisperCli = Find-WhisperCli
}
if ($whisperCli) { Report "whisper-cli OK：$whisperCli"; Update-ConfigValue -Name 'whisper_cli_path' -Value $whisperCli } else { Report 'FAIL 找不到 whisper-cli.exe' }
Update-ConfigValue -Name 'workspace_dir' -Value $ProjectRoot
Update-ConfigValue -Name 'ollama_api_url' -Value 'http://127.0.0.1:11434/api/generate'
Update-ConfigValue -Name 'vad_model_path' -Value (Join-Path $ProjectRoot 'models\ggml-silero-v6.2.0.bin')
Update-ConfigValue -Name 'whisper_chunk_seconds' -Value 300

$modelPath = Join-Path $ProjectRoot 'models\ggml-large-v3.bin'
$modelOk = $false
if (Test-Path -LiteralPath $modelPath) {
    $size = (Get-Item -LiteralPath $modelPath).Length
    if ($size -gt 2000000000) { $modelOk = $true; Report ('Whisper 模型已存在：{0:N2} GB' -f ($size / 1GB)) }
}
if (-not $modelOk) {
    $url = 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin?download=true'
    $partial = $modelPath + '.partial'
    Report "下載 Whisper large-v3 模型到 $modelPath"
    Invoke-External -Exe 'curl.exe' -Arguments @('-L','--fail','--continue-at','-','--output',$partial,$url) -Label '下載 Whisper large-v3' | Out-Null
    if (Test-Path -LiteralPath $partial) {
        $size = (Get-Item -LiteralPath $partial).Length
        if ($size -gt 2000000000) {
            Move-Item -LiteralPath $partial -Destination $modelPath -Force
            Report ('Whisper 模型下載完成：{0:N2} GB' -f ($size / 1GB))
        } else { Report ('FAIL Whisper 模型檔太小：{0:N2} MB' -f ($size / 1MB)) }
    } else { Report 'FAIL Whisper 模型下載失敗' }
}

$vadModelPath = Join-Path $ProjectRoot 'models\ggml-silero-v6.2.0.bin'
if (-not (Test-Path -LiteralPath $vadModelPath) -or (Get-Item -LiteralPath $vadModelPath).Length -ne 885098) {
    $vadUrl = 'https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v6.2.0.bin'
    $vadPartial = $vadModelPath + '.partial'
    Report "下載 Silero VAD 模型到 $vadModelPath"
    Invoke-External -Exe 'curl.exe' -Arguments @('-L','--fail','--output',$vadPartial,$vadUrl) -Label '下載 Silero VAD' | Out-Null
    if ((Test-Path -LiteralPath $vadPartial) -and (Get-Item -LiteralPath $vadPartial).Length -eq 885098) {
        Move-Item -LiteralPath $vadPartial -Destination $vadModelPath -Force
        Report 'Silero VAD 模型下載完成。'
    } else { Report 'FAIL Silero VAD 模型下載失敗或大小不符。' }
}

[Environment]::SetEnvironmentVariable('OLLAMA_MODELS', $OllamaModels, 'User')
$env:OLLAMA_MODELS = $OllamaModels
Report "OLLAMA_MODELS 設定為：$OllamaModels"
if (-not (Has-Command 'ollama')) {
    Install-WingetPackage -Id 'Ollama.Ollama' -Name 'Ollama' -TimeoutSeconds 1800 | Out-Null
    Refresh-Path
}
$ollamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
if ($ollamaCmd) {
    Report "Ollama OK：$($ollamaCmd.Source)"
    Stop-Process -Name ollama -Force -ErrorAction SilentlyContinue
    Start-Process -FilePath $ollamaCmd.Source -ArgumentList 'serve' -WindowStyle Hidden | Out-Null
    Start-Sleep -Seconds 8
    $listText = (& ollama list 2>$null) -join "`n"
    if ($listText -match 'qwen3:30b') {
        Report 'qwen3:30b 已存在。'
    } else {
        Report '開始下載 qwen3:30b，這可能非常久。'
        & ollama pull qwen3:30b 2>&1 | ForEach-Object { Report $_.ToString() }
        $listText = (& ollama list 2>$null) -join "`n"
        if ($listText -match 'qwen3:30b') { Report 'qwen3:30b 下載完成。' } else { Report 'WARN qwen3:30b 尚未完成或清單看不到。' }
    }
} else { Report 'WARN Ollama 未安裝成功，AI 校稿功能停用。' }

Report '安裝/檢查結束。'
