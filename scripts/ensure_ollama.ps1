function Ensure-OllamaReady {
    param($Config, [int]$WaitSeconds = 30)
    $api = [uri](Get-ConfigValue $Config 'ollama_api_url' 'http://127.0.0.1:11434/api/generate')
    if (-not $api.IsLoopback -or $api.Scheme -ne 'http') {
        throw 'Automatic startup only supports a local HTTP Ollama endpoint.'
    }
    $tagsUrl = $api.GetLeftPart([System.UriPartial]::Authority) + '/api/tags'
    $tags = $null
    try { $tags = Invoke-RestMethod -Uri $tagsUrl -TimeoutSec 2 } catch {}
    if ($null -eq $tags) {
        $exe = Get-ConfigValue $Config 'ollama_path' ''
        if (-not $exe -or -not (Test-Path -LiteralPath $exe)) {
            $command = Get-Command ollama.exe -ErrorAction SilentlyContinue
            if (-not $command) { throw 'Ollama is not installed; transcription remains available.' }
            $exe = $command.Source
        }
        $models = Get-ConfigValue $Config 'ollama_models_dir' (Join-Path $ProjectRoot 'models\ollama')
        if (-not (Test-Path -LiteralPath $models -PathType Container)) {
            throw 'Local Ollama models directory is missing; connect to the Internet to install models first.'
        }
        # Process-local environment only: do not change other projects or Windows settings.
        $oldModels = $env:OLLAMA_MODELS
        $oldHost = $env:OLLAMA_HOST
        try {
            $env:OLLAMA_MODELS = $models
            $env:OLLAMA_HOST = $api.GetLeftPart([System.UriPartial]::Authority)
            $stamp = Get-Date -Format 'yyyyMMdd_HHmmss_fff'
            Start-Process -FilePath $exe -ArgumentList 'serve' -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput (Join-Path $ProjectRoot "logs\ollama_$stamp.out.log") `
                -RedirectStandardError (Join-Path $ProjectRoot "logs\ollama_$stamp.err.log") | Out-Null
        } finally {
            $env:OLLAMA_MODELS = $oldModels
            $env:OLLAMA_HOST = $oldHost
        }
        $deadline = (Get-Date).AddSeconds($WaitSeconds)
        do {
            try { $tags = Invoke-RestMethod -Uri $tagsUrl -TimeoutSec 2; break } catch {}
            Start-Sleep -Milliseconds 500
        } while ((Get-Date) -lt $deadline)
        if ($null -eq $tags) { throw 'Ollama did not become ready within the startup timeout; see logs/ollama_*.err.log.' }
    }
    $model = Get-ConfigValue $Config 'ollama_model' 'qwen3:8b'
    if (@($tags.models | Where-Object { $_.name -eq $model -or $_.model -eq $model }).Count -eq 0) {
        throw "Local Ollama model is unavailable: $model. No automatic download or model substitution was performed."
    }
    return $true
}
