"""Windows watcher regression: unavailable optional model must not repeat jobs."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt', 'Windows runtime test')
class WatcherStartupTests(unittest.TestCase):
    def test_nested_same_names_mirror_outputs_and_reuse_across_folders(self):
        with tempfile.TemporaryDirectory(prefix='nested_', dir=ROOT / 'test_artifacts') as temp:
            case = Path(temp)
            source, output, workspace = case / 'input', case / 'output', case / 'workspace'
            final = workspace / 'output_final'
            for folder in (source, output, final):
                folder.mkdir(parents=True)
            config = json.loads((ROOT / 'config.local.json').read_text(encoding='utf-8-sig'))
            config.update(workspace_dir=str(workspace), desktop_input_dir=str(source),
                          desktop_output_dir=str(output), use_ollama_cleanup='auto',
                          ollama_api_url='http://127.0.0.1:1/api/generate',
                          ollama_models_dir=str(case / 'missing_models'),
                          desktop_outputs=['timeline', 'txt', 'srt', 'review', 'cleaned'])
            config_path = case / 'config.json'
            config_path.write_text(json.dumps(config), encoding='utf-8')
            for relative, wording in [('課程甲/同名', '甲課程'), ('課程乙/第二層/同名', '乙課程')]:
                media = source / (relative + '.mp4')
                media.parent.mkdir(parents=True)
                media.write_bytes(wording.encode())
                target = final / (relative + '.txt')
                target.parent.mkdir(parents=True)
                target.write_text(wording, encoding='utf-8')
                target.with_suffix('.srt').write_text(f'1\n00:00:00,000 --> 00:00:30,000\n{wording}\n', encoding='utf-8')
                target.with_name(target.stem + '.cleaned.txt').write_text(wording + '校稿候選', encoding='utf-8')
            # A root transcript must not suppress nested media sharing its name.
            (output / '同名.txt').write_text('原本根目錄人工稿', encoding='utf-8')
            duplicate = source / '複製課/同名.mp4'
            duplicate.parent.mkdir()
            duplicate.write_bytes((source / '課程甲/同名.mp4').read_bytes())
            env = dict(os.environ, BROADCAST_PACKAGE_INSTANCE='nested-test-' + case.name)
            command = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                       str(ROOT / 'scripts/run_transcribe.ps1'), '-ConfigPath', str(config_path),
                       '-Once', '-StableMinutes', '0']
            result = subprocess.run(command, env=env, capture_output=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout.decode('utf-8', errors='replace'))
            snapshots = {}
            for relative in ('課程甲/同名', '課程乙/第二層/同名', '複製課/同名'):
                transcript = output / (relative + '.txt')
                self.assertEqual(transcript.read_bytes(), (final / (relative + '.srt')).read_bytes())
                self.assertTrue((output / (relative + '.審查報告.txt')).exists())
                self.assertTrue((output / (relative + '.純文字.txt')).exists())
                self.assertEqual((output / (relative + '.cleaned.txt')).read_bytes(), (final / (relative + '.cleaned.txt')).read_bytes())
                snapshots[relative] = transcript.stat().st_mtime_ns
            self.assertNotEqual((output / '課程甲/同名.txt').read_bytes(), (output / '課程乙/第二層/同名.txt').read_bytes())
            self.assertEqual((output / '課程甲/同名.txt').read_bytes(), (output / '複製課/同名.txt').read_bytes())
            self.assertFalse(list(output.rglob('*轉錄進度*')))
            self.assertFalse(list(output.rglob('*轉錄中*')))
            self.assertEqual((output / '同名.txt').read_text(encoding='utf-8'), '原本根目錄人工稿')
            subprocess.run(command, env=env, capture_output=True, timeout=90, check=True)
            for relative, modified in snapshots.items():
                self.assertEqual((output / (relative + '.txt')).stat().st_mtime_ns, modified)

    def test_default_skips_existing_and_only_fills_missing_transcripts(self):
        with tempfile.TemporaryDirectory(prefix='missing_', dir=ROOT / 'test_artifacts') as temp:
            case = Path(temp)
            source, output, workspace = case / 'input', case / 'output', case / 'workspace'
            final = workspace / 'output_final'
            for folder in (source, output, final):
                folder.mkdir(parents=True, exist_ok=True)
            config = json.loads((ROOT / 'config.local.json').read_text(encoding='utf-8-sig'))
            config.update(workspace_dir=str(workspace), desktop_input_dir=str(source),
                          desktop_output_dir=str(output), use_ollama_cleanup='false')
            config_path = case / 'config.json'
            config_path.write_text(json.dumps(config), encoding='utf-8')
            for name in ('existing', 'subtitle', 'restore', 'empty', 'partial', 'new'):
                (source / f'{name}.mp3').write_bytes(b'invalid-media-' + name.encode())
            (output / 'existing.txt').write_text('Owner existing text, do not overwrite.', encoding='utf-8')
            (output / 'subtitle.srt').write_text('1\n00:00:00,000 --> 00:00:02,000\n既有字幕\n', encoding='utf-8')
            before = (output / 'existing.txt').stat().st_mtime_ns
            (output / 'empty.txt').touch()
            (output / 'partial.轉錄中.txt').write_text('partial, not complete', encoding='utf-8')
            (output / 'new.審查報告.txt').write_text('report, not transcript', encoding='utf-8')
            (final / 'restore.txt').write_text('補回已有結果。\n', encoding='utf-8')
            (final / 'restore.srt').write_text('1\n00:00:00,000 --> 00:00:02,000\n補回已有結果。\n', encoding='utf-8')
            # Run twice: skip must persist across restarts, not just one Run's cache.
            for _ in range(2):
                result = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                         '-File', str(ROOT / 'scripts/run_transcribe.ps1'), '-ConfigPath', str(config_path),
                                         '-Once', '-StableMinutes', '0'], stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=60)
                self.assertEqual(result.returncode, 1)  # Three intentionally invalid NEW media.
                self.assertEqual((output / 'existing.txt').stat().st_mtime_ns, before)
            statuses = [json.loads(p.read_text(encoding='utf-8-sig')) for p in (workspace / 'logs/status').glob('*.json')]
            by_name = {Path(s['input_path']).stem: s for s in statuses}
            self.assertEqual(set(by_name), {'restore', 'empty', 'partial', 'new'})
            self.assertEqual(by_name['restore']['current_status'], 'completed')
            self.assertEqual(sum(s['status'] == 'queued' for s in by_name['restore']['status_history']), 1)
            for name in ('empty', 'partial', 'new'):
                self.assertEqual(by_name[name]['current_status'], 'failed')
            self.assertEqual((output / 'restore.txt').read_bytes(), (final / 'restore.srt').read_bytes())
            self.assertFalse(list(final.glob('*.cleaned.txt')))

    def test_unavailable_cleanup_does_not_repeat_completed_jobs(self):
        # Missing local executable is deterministic; do not depend on Internet or
        # stop the user's Ollama. Port 1 is not the production endpoint.
        with tempfile.TemporaryDirectory(prefix='startup_', dir=ROOT / 'test_artifacts') as temp:
            case = Path(temp)
            source = case / 'input'
            output = case / 'output'
            workspace = case / 'workspace'
            final = workspace / 'output_final'
            for folder in (source, output, final):
                folder.mkdir(parents=True, exist_ok=True)
            config = json.loads((ROOT / 'config.local.json').read_text(encoding='utf-8-sig'))
            config.update(workspace_dir=str(workspace), desktop_input_dir=str(source),
                          desktop_output_dir=str(output), ollama_api_url='http://127.0.0.1:1/api/generate',
                          ollama_models_dir=str(case / 'missing_models'), use_ollama_cleanup='auto')
            config_path = case / 'config.json'
            config_path.write_text(json.dumps(config), encoding='utf-8')

            def add_file(name):
                # Existing final outputs bypass ASR; source content is never decoded.
                (final / f'{name}.txt').write_text('測試啟動流程。\n', encoding='utf-8')
                (final / f'{name}.srt').write_text('1\n00:00:00,000 --> 00:00:02,000\n測試啟動流程。\n', encoding='utf-8')
                (source / f'{name}.mp3').write_bytes(b'fixture-' + name.encode())

            add_file('first')
            env = dict(os.environ, PYTHONIOENCODING='utf-8')
            process = subprocess.Popen(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                        '-File', str(ROOT / 'scripts/run_transcribe.ps1'),
                                        '-ConfigPath', str(config_path), '-StableMinutes', '0', '-PollSeconds', '1', '-RetryCleanup'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            try:
                def wait_output(name):
                    deadline = time.monotonic() + 45
                    while time.monotonic() < deadline:
                        if (final / f'{name}.quality.json').exists():
                            return
                        self.assertIsNone(process.poll(), 'watcher exited unexpectedly')
                        time.sleep(.2)
                    self.fail(f'no output for {name}')

                wait_output('first')
                add_file('second')
                wait_output('second')
                (source / 'duplicate.mp3').write_bytes((source / 'first.mp3').read_bytes())
                wait_output('duplicate')
                time.sleep(3)
                statuses = list((workspace / 'logs/status').glob('*.json'))
                self.assertEqual(len(statuses), 3)
                for path in statuses:
                    status = json.loads(path.read_text(encoding='utf-8-sig'))
                    self.assertEqual(status['current_status'], 'completed_with_warning')
                    self.assertEqual(sum(s['status'] == 'queued' for s in status['status_history']), 1)
                self.assertFalse(list(final.glob('*.cleaned.txt')))
                for name in ('first', 'second', 'duplicate'):
                    self.assertEqual((output / f'{name}.txt').read_bytes(), (final / f'{name}.srt').read_bytes())
            finally:
                process.terminate()
                process.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
