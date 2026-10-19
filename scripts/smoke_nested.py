"""Real two-chunk MP4 smoke for recursive input and mirrored output."""
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    case = ROOT / 'test_artifacts' / time.strftime('nested_mp4_%Y%m%d_%H%M%S')
    relative = Path('【課程測試】/第二層/同名課程')
    source = case / 'input' / relative.with_suffix('.mp4')
    source.parent.mkdir(parents=True)
    output = case / 'output'
    output.mkdir()
    config = json.loads((ROOT / 'config.local.json').read_text(encoding='utf-8-sig'))
    fixture = ROOT / 'test_artifacts/smoke_20260922_075622/input/smoke_input.wav'
    subprocess.run([config['ffmpeg_path'], '-hide_banner', '-loglevel', 'error', '-i', str(fixture),
                    '-c:a', 'aac', str(source)], check=True)
    config.update(workspace_dir=str(case / 'workspace'), desktop_input_dir=str(case / 'input'),
                  desktop_output_dir=str(output), use_ollama_cleanup='false', whisper_chunk_seconds=40,
                  desktop_outputs=['timeline', 'srt', 'txt', 'review'])
    config_path = case / 'config.json'
    config_path.write_text(json.dumps(config), encoding='utf-8')
    env = dict(os.environ, PYTHONIOENCODING='utf-8', BROADCAST_PACKAGE_INSTANCE=case.name)
    command = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
               str(ROOT / 'scripts/run_transcribe.ps1'), '-ConfigPath', str(config_path),
               '-Once', '-StableMinutes', '0']
    with (case / 'run.log').open('wb') as log:
        result = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=1200)
    final = case / 'workspace/output_final' / relative.with_suffix('.srt')
    timeline = output / relative.with_suffix('.txt')
    assert result.returncode == 0
    assert timeline.read_bytes() == final.read_bytes()
    assert (case / 'workspace/input_audio' / relative.with_suffix('.mp4')).read_bytes() == source.read_bytes()
    assert (output / (str(relative) + '.審查報告.txt')).exists()
    assert (output / (str(relative) + '.純文字.txt')).exists()
    checkpoints = [p for p in (case / 'workspace/whisper_raw/jobs').rglob('*.complete') if not p.name.endswith('.wav.complete')]
    assert len(checkpoints) == 2
    before = {str(p): p.stat().st_mtime_ns for p in checkpoints + [timeline]}
    subprocess.run(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=True)
    assert all(Path(p).stat().st_mtime_ns == modified for p, modified in before.items())
    assert not list(output.rglob('*轉錄中*')) and not list(output.rglob('*轉錄進度*'))
    report = dict(status='PASS', real_mp4=True, asr_checkpoints=2, nested_timeline_equal_srt=True,
                  nested_review=True, nested_plain=True, restart_preserves_outputs_and_checkpoints=True,
                  output=str(timeline), cleanup_disabled_for_path_smoke=True)
    (case / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
