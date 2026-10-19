# Local models

Model binaries are intentionally excluded from Git. Run `scripts/install_windows.ps1` or copy the validated local package to provision them.

Required runtime models:

- `ggml-large-v3.bin` — Whisper speech-to-text model.
- `ggml-silero-v6.2.0.bin` — Silero voice activity detection model; filters music and non-speech before Whisper.

Exact sizes, hashes, sources, and runtime versions are locked in `config/model-manifest.json`.
