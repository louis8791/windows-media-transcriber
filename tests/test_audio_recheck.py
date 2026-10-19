import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from clean_transcript import validate_response
from launch_menu import LANGUAGES, WizardMenu, make_session
from recheck_audio import map_cues, parse_srt_strict, qwen_differences, run, select_regions, text_differences


class AudioRecheckTests(unittest.TestCase):
    def test_english_cleanup_rejects_translation_negation_numbers_and_marker_changes(self):
        source = 'Dear friends, no one should escape justice. There were 7,000 people. [unclear 01]'
        self.assertEqual(validate_response({'response': source}, source), source)
        for output in ('親愛的朋友，所有人都應遵守正義。共有 7,000 人。[unclear 01]',
                       source.replace('no one', 'everyone'), source.replace('7,000', '8,000'),
                       source.replace('no one', 'not one'), source.replace('[unclear 01]', '')):
            with self.subTest(output=output), self.assertRaises(RuntimeError):
                validate_response({'response': output}, source)

    def test_regions_include_missing_opening_ending_and_boundaries_and_report_budget(self):
        cues = [{'start': '00:00:25,000', 'end': '00:01:00,000', 'number': 1, 'text': 'Dear friends'},
                {'start': '00:02:00,000', 'end': '00:03:00,000', 'number': 2, 'text': 'Some speech'}]
        selected, pending = select_regions(cues, 720_000, 300)
        self.assertEqual(selected[0]['start_ms'], 0)
        self.assertTrue(selected[0]['reason'].startswith('無字幕區段'))
        self.assertLessEqual(len(selected), 4)
        self.assertTrue(all(r['end_ms'] - r['start_ms'] <= 60_000 for r in selected + pending))
        self.assertTrue(any(r['reason'] == '分段交界' for r in selected + pending))
        self.assertTrue(pending)

    def test_opening_gap_is_not_skipped_by_broad_opening_region(self):
        cues = [{'start': '00:00:01,120', 'end': '00:00:13,890', 'number': 1, 'text': 'Introduction'},
                {'start': '00:00:40,670', 'end': '00:01:19,610', 'number': 2, 'text': 'Speech'}]
        selected, _ = select_regions(cues, 80_000, 300)
        self.assertEqual(selected[0], {'start_ms': 8890, 'end_ms': 45670, 'reason': '無字幕區段，可能是靜音或漏辨'})
        self.assertEqual(map_cues([{'start_ms': 1000, 'end_ms': 2000, 'text': 'Greeting'}], selected[0], 1.0)[0]['start_ms'], 9890)

    def test_slow_timestamps_map_to_source_without_dropping_crossing_cues(self):
        cues = parse_srt_strict('1\n00:00:10,000 --> 00:00:20,000\nFull sentence\n')
        mapped = map_cues(cues, {'start_ms': 100_000, 'end_ms': 112_000})
        self.assertEqual(mapped, [{'start_ms': 108_000, 'end_ms': 112_000, 'text': 'Full sentence'}])

    def test_qwen_cannot_invent_difference_evidence(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self):
                return json.dumps({'response': json.dumps({'differences': [{'primary': 'not spoken', 'recheck': 'new guess'}]})}).encode()
        with patch('recheck_audio.urllib.request.urlopen', return_value=Response()):
            with self.assertRaises(ValueError):
                qwen_differences('Dear friends', 'Dear Dr Hong, dear friends', {})

    def test_exact_diff_has_no_identical_pairs_and_qwen_can_only_select_existing_ids(self):
        primary, alternate = 'No one was sentenced. Dr Ong.', 'One was sentenced. Dr Hong.'
        choices = text_differences(primary, alternate)
        self.assertTrue(choices)
        self.assertTrue(all(d['primary'] != d['recheck'] and d['primary'] in primary and d['recheck'] in alternate for d in choices))
        class Response:
            ids = [0]
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps({'response': json.dumps({'review_ids': self.ids})}).encode()
        response = Response()
        with patch('recheck_audio.urllib.request.urlopen', return_value=response):
            selected = qwen_differences(primary, alternate, {})
            self.assertEqual(selected['differences'], [choices[0]])
            response.ids = [999]
            with self.assertRaises(ValueError):qwen_differences(primary, alternate, {})

    def test_failed_recheck_keeps_primary_and_reports_failure_without_complete_markers(self):
        with tempfile.TemporaryDirectory(prefix='recheck_', dir=ROOT/'test_artifacts') as directory:
            case = Path(directory)
            wav = case/'source.wav'
            with wave.open(str(wav), 'wb') as stream:
                stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                stream.writeframes(b'\0\0' * 16000)
            srt = case/'primary.srt'
            original = '1\n00:00:00,000 --> 00:00:01,000\nDear friends\n'
            srt.write_text(original, encoding='utf-8')
            config = json.loads((ROOT/'config.local.json').read_text(encoding='utf-8-sig'))
            config['language'] = 'en'
            with patch('recheck_audio.subprocess.run', side_effect=RuntimeError('ASR unavailable')):
                report = run(config, wav, srt, case/'output', 300, False)
            self.assertEqual(report['status'], 'completed_with_warning')
            self.assertTrue(report['errors'])
            self.assertEqual(srt.read_text(encoding='utf-8'), original)
            self.assertFalse(list((case/'output').glob('*.complete')))


if __name__ == '__main__':
    unittest.main()
