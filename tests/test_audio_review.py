import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from audio_review import uncovered, speech_without_text, matching_media, matching_subtitle, matching_timed_transcript, check, make_clip
from launch_menu import WizardMenu
import tkinter as tk


class AudioReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ROOT/'work/audio_review').mkdir(parents=True, exist_ok=True)
    def test_gap_union_includes_opening_ending_and_overlapping_cues(self):
        cues = [{'start_ms': 2000, 'end_ms': 7000}, {'start_ms': 3000, 'end_ms': 5000},
                {'start_ms': 9000, 'end_ms': 10000}]
        self.assertEqual(uncovered(cues, 12000), [(0, 2000), (7000, 9000), (10000, 12000)])

    def test_mismatched_audio_duration_is_rejected(self):
        with self.assertRaises(ValueError):
            uncovered([{'start_ms': 9000, 'end_ms': 12000}], 10000)

    def test_speech_intersection_does_not_label_existing_text_or_brief_noise(self):
        rows = speech_without_text([(0, 2000), (4000, 6000)], [(1000, 5000), (5800, 6000)])
        self.assertEqual([(row['start_ms'], row['end_ms']) for row in rows], [(1000, 2000), (4000, 5000)])
        self.assertTrue(all(row['kind'] == 'possible_omission' for row in rows))

    def test_exact_relative_source_only_and_ambiguous_extensions_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test_artifacts') as temp:
            p = Path(temp)
            for folder in ('input/A', 'input/B', 'output/A'):
                (p/folder).mkdir(parents=True)
            config = {'desktop_input_dir': str(p/'input'), 'desktop_output_dir': str(p/'output')}
            a, b = p/'input/A/same.wav', p/'input/B/same.wav'
            a.write_bytes(b'a'); b.write_bytes(b'b')
            self.assertEqual(matching_media(p/'output/A/same.srt', config), a)
            self.assertIsNone(matching_media(p/'output/A/renamed.srt', config))
            (p/'input/A/same.mp3').write_bytes(b'c')
            self.assertIsNone(matching_media(p/'output/A/same.srt', config))

    def test_failed_scan_never_publishes_success_report(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'work/audio_review') as temp:
            p=Path(temp);media=p/'a.wav';srt=p/'a.srt'
            media.write_bytes(b'fixture');srt.write_text('1\n00:00:00,000 --> 00:00:01,000\nHello\n', encoding='utf-8')
            with patch('audio_review.check_signature',return_value={'algorithm':'test'}),patch('audio_review.duration', return_value=5000), patch('audio_review.Silero', side_effect=ValueError('model fail')):
                with self.assertRaises(ValueError):check(media,srt,{},p/'report.json')
            self.assertFalse((p/'report.json').exists())

    def test_clip_refuses_to_overwrite_source_or_write_outside_its_cache(self):
        with self.assertRaises(ValueError):make_clip('source.wav',0,1000,1,{},ROOT/'README.md')

    def test_slow_playback_limits_input_before_time_stretch_to_preserve_tail(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'work/audio_review') as temp:
            with patch('audio_review.ffmpeg',return_value='ffmpeg.exe'),patch('audio_review.run') as execute:
                make_clip('source.wav',1000,7000,.75,{},Path(temp)/'slow.wav')
            args=execute.call_args.args[0]
            self.assertLess(args.index('-t'),args.index('-i'))
            self.assertEqual(args[args.index('-t')+1],'6.0')
            self.assertIn('atempo=0.75',args)

    def test_source_changed_during_scan_is_not_published(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'work/audio_review') as temp:
            p=Path(temp);media=p/'a.wav';srt=p/'a.srt'
            media.write_bytes(b'original');srt.write_text('',encoding='utf-8')
            def change(*_):media.write_bytes(b'changed');return []
            with patch('audio_review.check_signature',return_value={'algorithm':'test'}),patch('audio_review.duration',return_value=2000),patch('audio_review.Silero') as vad,patch('audio_review.run',return_value=bytes(128000)),patch('audio_review.ffmpeg',return_value='ffmpeg.exe'):
                vad.return_value.detect.side_effect=change
                with self.assertRaisesRegex(ValueError,'來源改變'):check(media,srt,{},p/'report.json')
            self.assertFalse((p/'report.json').exists())

    def test_plain_text_matches_exact_content_only_and_refuses_two_sources(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test_artifacts') as temp:
            p=Path(temp)
            for folder in ('input','output','output_final'):(p/folder).mkdir()
            config={'desktop_input_dir':str(p/'input'),'desktop_output_dir':str(p/'output'),'workspace_dir':str(p)}
            (p/'input/a.wav').write_bytes(b'a')
            srt=p/'output_final/a.srt';srt.write_text('1\n00:00:01,000 --> 00:00:02,000\nHello, friend.\n',encoding='utf-8')
            plain=p/'output/renamed.txt';plain.write_text('Hello,\nfriend.\n',encoding='utf-8')
            self.assertEqual(matching_timed_transcript(plain,config),srt)
            plain.write_text('Hello,friend.',encoding='utf-8')
            self.assertIsNone(matching_timed_transcript(plain,config))
            plain.write_text('Hello, friends.',encoding='utf-8')
            self.assertIsNone(matching_timed_transcript(plain,config))
            plain.write_text('Hello, friend.',encoding='utf-8')
            (p/'input/b.wav').write_bytes(b'b')
            (p/'output_final/b.srt').write_bytes(srt.read_bytes())
            self.assertIsNone(matching_timed_transcript(plain,config))

    def test_saved_check_reused_and_force_or_changed_inputs_invalidate(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test_artifacts') as temp:
            p=Path(temp);out=p/'work/audio_review';out.mkdir(parents=True)
            media=p/'a.wav';srt=p/'a.srt';media.write_bytes(b'first');srt.write_text('',encoding='utf-8')
            with patch('audio_review.ROOT',p),patch('audio_review.check_signature',return_value={'algorithm':'test'}),patch('audio_review.duration',return_value=4000),patch('audio_review.ffmpeg',return_value='ffmpeg.exe'),patch('audio_review.run',return_value=bytes(64000)),patch('audio_review.Silero') as vad:
                vad.return_value.detect.return_value=[(1000,2000)]
                first=check(media,srt,{},out/'first.json');self.assertFalse(first['reused'])
                again=check(media,srt,{},out/'again.json');self.assertTrue(again['reused'])
                self.assertEqual(first['findings'],again['findings']);self.assertEqual(vad.call_count,1)
                forced=check(media,srt,{},out/'forced.json',force=True);self.assertFalse(forced['reused'])
                media.write_bytes(b'changed')
                changed=check(media,srt,{},out/'changed.json');self.assertFalse(changed['reused'])
                self.assertEqual(vad.call_count,3)
                srt.write_text('1\n00:00:00,000 --> 00:00:00,500\nHello\n',encoding='utf-8')
                self.assertFalse(check(media,srt,{},out/'edited.json')['reused'])
                cache=next((out/'cache').glob('*.json'))
                for path in (out/'cache').glob('*.json'):path.write_text('{broken',encoding='utf-8')
                self.assertFalse(check(media,srt,{},out/'corrupt.json')['reused'])
                self.assertEqual(vad.call_count,5)
                with patch('audio_review.check_signature',return_value={'algorithm':'new'}) as signature:
                    self.assertFalse(check(media,srt,{},out/'newalgo.json')['reused'])
                    self.assertEqual(vad.call_count,6)

    def test_wizard_routes_review_without_transcription_and_closes_on_navigation(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test_artifacts') as temp:
            p=Path(temp);(p/'input').mkdir();(p/'output').mkdir()
            audio=p/'input/a.wav';audio.write_bytes(b'fixture')
            config={'desktop_input_dir':str(p/'input'),'desktop_output_dir':str(p/'output'),'workspace_dir':str(p)}
            w=tk.Tk();w.withdraw()
            try:
                menu=WizardMenu(w,config);menu.choose('gapcheck');menu.selection={audio}
                with patch.object(menu.audio_review,'load',return_value=True) as load, patch.object(menu,'execute') as execute:
                    menu.next();load.assert_called_once_with(audio,True);execute.assert_not_called()
                    self.assertEqual(menu.page,'audio_review')
                with patch.object(menu.audio_review,'close') as close:
                    menu.show_page('home');close.assert_called_once()
            finally:w.destroy()


if __name__ == '__main__':unittest.main()
