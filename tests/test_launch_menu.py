"""Behavior checks for the click-to-start menu and exact-path selection."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from launch_menu import LaunchMenu, WizardMenu, make_session, run_request, scan_files, utility_result, CATEGORIES


@unittest.skipUnless(os.name == 'nt', 'Windows menu')
class LaunchMenuTests(unittest.TestCase):
    def test_wizard_english_recheck_selection_is_in_confirmation_and_actual_session(self):
        root = self.window()
        menu = WizardMenu(root, self.config)
        menu.choose('force')
        root.update()
        self.assertEqual(menu.mode.get(), 'force')
        menu.selection = {self.paths[0]}
        menu.language.set('英文（保留英文原文）')
        menu.recheck.set(True)
        menu.show_page('confirm')
        self.assertIn('英文（保留英文原文）', menu.confirm_text.cget('text'))
        self.assertIn('自動重查疑點：開啟', menu.confirm_text.cget('text'))
        with patch.object(menu, 'execute'):
            menu.begin()
        session = json.loads(menu.request[1].read_text(encoding='utf-8'))
        self.assertEqual(session['language'], 'en')
        self.assertTrue(session['asr_recheck'])
        self.assertEqual(self.config['language'], 'zh')
        retry, _ = make_session(self.config, 'retry', [self.paths[0]], False, False, ['timeline'], 'en', True)
        self.assertFalse(retry['asr_recheck'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='menu_', dir=ROOT / 'test_artifacts')
        self.addCleanup(self.temp.cleanup)
        self.case = Path(self.temp.name)
        self.input, self.output, self.workspace = self.case / 'input', self.case / 'output', self.case / 'workspace'
        for directory in (self.input, self.output, self.workspace / 'output_final'):
            directory.mkdir(parents=True)
        self.config = json.loads((ROOT / 'config.local.json').read_text(encoding='utf-8-sig'))
        self.config.update(desktop_input_dir=str(self.input), desktop_output_dir=str(self.output),
                           workspace_dir=str(self.workspace), desktop_outputs=['timeline'], use_ollama_cleanup='false')
        self.paths = []
        for folder in ('A', 'B'):
            path = self.input / folder / 'same.mp3'
            path.parent.mkdir()
            path.write_bytes(folder.encode())
            self.paths.append(path)
            final = self.workspace / 'output_final' / folder
            final.mkdir()
            (final / 'same.txt').write_text(f'這是{folder}的文字。\n', encoding='utf-8')
            (final / 'same.srt').write_text(f'1\n00:00:00,000 --> 00:00:02,000\n這是{folder}的文字。\n', encoding='utf-8')

    def window(self):
        root = tk.Tk()
        root.withdraw()
        def close():
            try:
                root.destroy()
            except tk.TclError:
                pass
        self.addCleanup(close)
        return root

    def test_modes_require_explicit_files_and_do_not_mutate_defaults(self):
        original = json.dumps(self.config, sort_keys=True)
        for mode, switch in (('force', '-Force'), ('retry', '-RetryCleanup')):
            with self.assertRaises(ValueError):
                make_session(self.config, mode, self.paths, True, False, ['timeline'])
            with self.assertRaises(ValueError):
                make_session(self.config, mode, [], False, False, ['timeline'])
            session, args = make_session(self.config, mode, [self.paths[0]], False, True, ['timeline'])
            self.assertEqual(session['selected_input_paths'], [str(self.paths[0].resolve())])
            self.assertIn(switch, args)
            self.assertIn('-Once', args)
            self.assertEqual(session['reuse_only'], mode == 'retry')
        self.assertEqual(json.dumps(self.config, sort_keys=True), original)
        _, args = make_session(self.config, 'normal', [], True, True, ['timeline'])
        self.assertEqual(args, [])
        missing = self.input / 'new.mp3'
        missing.write_bytes(b'new')
        with self.assertRaisesRegex(ValueError, '沒有可補回'):
            make_session(self.config, 'retry', [missing], False, False, ['timeline'])
        with self.assertRaises(ValueError):
            make_session(self.config, 'normal', [self.paths[0]], False, False, [])

    def test_actual_runner_selects_one_exact_path_among_same_names(self):
        session, args = make_session(self.config, 'normal', [self.paths[0]], False, False, ['timeline'])
        config = self.case / 'session.json'
        config.write_text(json.dumps(session), encoding='utf-8')
        result = run_request(('runner', config, args))
        self.assertEqual(result, 0)
        self.assertTrue((self.output / 'A/same.txt').is_file())
        self.assertFalse((self.output / 'B/same.txt').exists())
        statuses = list((self.workspace / 'logs/status').glob('*.json'))
        self.assertEqual(len(statuses), 1)
        self.assertEqual(json.loads(statuses[0].read_text(encoding='utf-8-sig'))['input_path'], str(self.paths[0]))

    def test_real_controls_force_selection_and_cancel_without_task(self):
        root = self.window()
        menu = LaunchMenu(root, self.config)
        menu.mode_buttons['force'].invoke()
        self.assertFalse(menu.all_media.get())
        self.assertTrue(menu.start_button.instate(['disabled']))
        menu.select_visible()
        self.assertFalse(menu.start_button.instate(['disabled']))
        self.assertIn('2 支', menu.summary.get())
        for variable in menu.outputs.values():
            variable.set(False)
        menu.update_state()
        self.assertTrue(menu.start_button.instate(['disabled']))
        menu.cancel()
        self.assertIsNone(menu.request)
        self.assertFalse((self.workspace / 'logs/launch_sessions').exists())

    def test_real_begin_writes_only_selected_session_and_retry_can_not_transcribe_new_media(self):
        root = self.window()
        menu = LaunchMenu(root, self.config)
        menu.mode_buttons['retry'].invoke()
        menu.selection = {self.paths[1]}
        menu.draw_files()
        with patch.object(menu, 'execute'):
            menu.start_button.invoke()
        self.assertEqual(menu.request[0], 'runner')
        session = json.loads(menu.request[1].read_text(encoding='utf-8'))
        self.assertEqual(session['selected_input_paths'], [str(self.paths[1])])
        self.assertTrue(session['reuse_only'])
        self.assertIn('-RetryCleanup', menu.request[2])

    def test_proofread_routes_text_without_asr_and_auxiliary_files_are_not_preselected(self):
        source = self.output / 'my renamed script.txt'
        source.write_text('這是文字。', encoding='utf-8')
        for name in ('my.cleaned.txt', 'my.校稿進度.txt', 'my.審查報告.txt', 'my.轉錄中.txt'):
            (self.output / name).write_text('auxiliary', encoding='utf-8')
        self.assertEqual(scan_files(self.output, True), [source])
        root = self.window()
        menu = LaunchMenu(root, self.config)
        menu.mode_buttons['proofread'].invoke()
        menu.select_visible()
        with patch.object(menu, 'execute'):
            menu.start_button.invoke()
        self.assertEqual(menu.request, ('proofread', [source]))
        with patch('launch_menu.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(run_request(menu.request), 0)
            command = run.call_args.args[0]
            self.assertIn(str(ROOT / 'scripts/proofread_only.py'), command)
            self.assertNotIn(str(ROOT / 'scripts/run_transcribe.py'), command)
            self.assertEqual(command[-1], str(source))

    def test_categories_and_utilities_preserve_sources(self):
        self.assertEqual(sum(map(len, CATEGORIES.values())), 14)
        source = self.output / 'subtitle.srt'
        source.write_text('1\n00:00:00,000 --> 00:00:02,000\n文字內容\n', encoding='utf-8')
        before = source.read_bytes()
        root = self.window()
        menu = LaunchMenu(root, self.config)
        menu.mode_buttons['plain'].invoke()
        menu.select_visible()
        menu.begin()
        self.assertTrue(root.winfo_exists())
        exported = list(self.output.glob('subtitle.純文字匯出.*.txt'))
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0].read_text(encoding='utf-8'), '文字內容\n')
        self.assertEqual(source.read_bytes(), before)
        menu.mode_buttons['settings'].invoke()
        menu.begin()
        self.assertIn(str(self.input), menu.result.get('1.0', 'end'))
        self.assertIsNone(menu.request)

    def test_background_work_keeps_window_and_reports_completion(self):
        root = self.window()
        menu = LaunchMenu(root, self.config)
        menu.request = ('runner', self.case / 'unused.json', [])
        command = [sys.executable, '-c', 'print("GUI progress")']
        with patch('launch_menu.request_commands', return_value=[command]):
            menu.execute()
            import time
            limit = time.monotonic() + 5
            while menu.running and time.monotonic() < limit:
                root.update()
                time.sleep(.02)
        self.assertFalse(menu.running)
        self.assertTrue(root.winfo_exists())
        self.assertIn('GUI progress', menu.result.get('1.0', 'end'))
        self.assertIn('退出碼 0', menu.result.get('1.0', 'end'))

    def test_stop_terminates_owned_background_process(self):
        root = self.window()
        menu = LaunchMenu(root, self.config)
        menu.request = ('runner', self.case / 'unused.json', [])
        import time
        with patch('launch_menu.request_commands', return_value=[[sys.executable, '-c', 'import time; time.sleep(30)']]):
            menu.execute()
            limit = time.monotonic() + 5
            while menu.process is None and time.monotonic() < limit:
                root.update()
                time.sleep(.02)
            process = menu.process
            self.assertIsNotNone(process)
            menu.stop()
            while menu.running and time.monotonic() < limit:
                root.update()
                time.sleep(.02)
        self.assertFalse(menu.running)
        self.assertIsNotNone(process.poll())
        self.assertTrue(root.winfo_exists())
        self.assertIn('工作已停止', menu.result.get('1.0', 'end'))

    def test_wizard_pages_and_confirmation_match_actual_outputs(self):
        root = self.window()
        menu = WizardMenu(root, self.config)
        self.assertEqual(menu.page, 'home')
        menu.choose('force')
        self.assertEqual(menu.page, 'files')
        menu.selection = {self.paths[0]}
        menu.draw_files()
        menu.next()
        self.assertEqual(menu.page, 'outputs')
        for token, variable in menu.outputs.items():
            variable.set(token == 'srt')
        menu.next()
        self.assertEqual(menu.page, 'confirm')
        summary = menu.confirm_text.cget('text')
        self.assertIn('SRT 字幕', summary)
        self.assertNotIn('含時間碼 TXT', summary)
        menu.back()
        self.assertEqual(menu.page, 'outputs')
        self.assertEqual(menu.selection, {self.paths[0]})
        menu.next()
        with patch.object(menu, 'execute'):
            menu.begin()
        session = json.loads(menu.request[1].read_text(encoding='utf-8'))
        self.assertEqual(session['desktop_outputs'], ['srt'])
        self.assertIn('-Force', menu.request[2])
        self.assertTrue(root.winfo_exists())

    def test_wizard_preview_and_plain_have_distinct_routes(self):
        root = self.window()
        menu = WizardMenu(root, self.config)
        source = self.output / 'example.srt'
        source.write_text('1\n00:00:00,000 --> 00:00:02,000\n2026\n', encoding='utf-8')
        menu.choices(False)
        menu.choose('preview')
        menu.selection = {source}
        menu.next()
        self.assertEqual(menu.page, 'result')
        self.assertIn('2026', menu.result.get('1.0', 'end'))
        menu.choose('plain')
        menu.selection = {source}
        menu.next()
        self.assertEqual(menu.page, 'confirm')
        self.assertFalse(list(self.output.glob('*.純文字匯出.*.txt')))
        menu.begin()
        self.assertEqual(menu.page, 'result')
        target = next(self.output.glob('*.純文字匯出.*.txt'))
        self.assertEqual(target.read_text(encoding='utf-8'), '2026\n')

    def test_wizard_background_runner_keeps_same_window(self):
        root = self.window()
        menu = WizardMenu(root, self.config)
        menu.choose('normal')
        menu.selection = {self.paths[0]}
        menu.all_media.set(False)
        menu.watch.set(False)
        menu.outputs['timeline'].set(True)
        menu.outputs['cleaned'].set(False)
        menu.begin()
        import time
        limit = time.monotonic() + 30
        while menu.running and time.monotonic() < limit:
            root.update()
            time.sleep(.02)
        self.assertFalse(menu.running)
        self.assertEqual(menu.page, 'result')
        self.assertTrue(root.winfo_exists())
        self.assertTrue((self.output / 'A/same.txt').exists())
        self.assertFalse((self.output / 'B/same.txt').exists())
        self.assertIn('退出碼 0', menu.result.get('1.0', 'end'))


if __name__ == '__main__':
    unittest.main()
