import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
sys.path.insert(0, str(ROOT))
import audio_review as ar
from tests.test_audio_review_workflow import AudioReviewWorkflowTests as Fixtures


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'test_artifacts')
        self.root = Path(self.temp.name)
        self.out = self.root/'work/audio_review'
        self.out.mkdir(parents=True)
        self.media, self.srt = self.root/'a.wav', self.root/'a.srt'
        self.media.write_bytes(b'audio');self.srt.write_text('', encoding='utf-8')
        self.patches = [patch('audio_review.ROOT', self.root),
            patch('audio_review.check_signature', return_value={'algorithm':'fixture'}),
            patch('audio_review.duration', return_value=600000),
            patch('audio_review.ffmpeg', return_value='fixture'),
            patch('audio_review.run', return_value=b'pcm'), patch('audio_review.Silero')]
        self.mocks = [item.start() for item in self.patches]
        self.vad = self.mocks[-1].return_value

    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup()

    def scan(self, name, force=False):
        return ar.check(self.media, self.srt, {}, self.out/(name+'.json'), force)

    def interrupt(self, name='failed', force=False):
        self.vad.detect.side_effect = [[(1000,2000)], RuntimeError('interrupted')]
        with self.assertRaises(RuntimeError):self.scan(name,force)
        self.assertFalse((self.out/(name+'.json')).exists())
        self.vad.detect.reset_mock();self.vad.detect.side_effect = lambda pcm, left: [(left+1000,left+2000)]

    def test_resume_skips_completed_chunk_and_matches_fresh(self):
        self.interrupt()
        resumed = self.scan('resumed')
        self.assertEqual((resumed['resumed_chunks'],resumed['checked_chunks']),(1,1))
        self.assertEqual(self.vad.detect.call_args.args[1],290000)
        self.assertEqual(self.vad.detect.call_count,1)
        fresh = self.scan('fresh',True)
        self.assertEqual(fresh['resumed_chunks'],0)
        self.assertEqual(resumed['findings'],fresh['findings'])

    def test_corrupt_chunk_is_recomputed(self):
        self.interrupt()
        next((self.out/'checkpoints').glob('*/*/000000000000.json')).write_text('{broken')
        result = self.scan('repaired')
        self.assertEqual((result['resumed_chunks'], result['checked_chunks']),(0,2))

    def test_changed_source_cannot_resume_old_chunks(self):
        self.interrupt();self.media.write_bytes(b'changed')
        self.assertEqual(self.scan('changed')['resumed_chunks'],0)

    def test_force_restarts_even_when_interrupted_and_tool_change_invalidates(self):
        self.interrupt()
        forced=self.scan('forced_fresh',True)
        self.assertEqual((forced['resumed_chunks'],forced['checked_chunks']),(0,2))
        self.interrupt('second_failure',True)
        with patch('audio_review.check_signature',return_value={'algorithm':'changed'}):
            self.assertEqual(self.scan('new_tool')['resumed_chunks'],0)

    def test_valid_empty_checkpoint_reused_but_invalid_boundaries_rejected(self):
        self.vad.detect.side_effect=[[],RuntimeError('interrupted')]
        with self.assertRaises(RuntimeError):self.scan('silent_partial')
        self.vad.detect.side_effect=lambda pcm,left: []
        self.assertEqual(self.scan('silent_resume')['resumed_chunks'],1)
        path=next((self.out/'checkpoints').glob('*/*/000000000000.json'))
        data=json.loads(path.read_text());data['speech']=[[0,300001]];path.write_text(json.dumps(data))
        self.assertIsNone(ar.checkpoint_speech(path,data['source_hashes'],data['check_signature'],600000,0,300000))

    def test_incomplete_forced_run_takes_priority_over_old_cache(self):
        self.vad.detect.side_effect = lambda pcm,left: [(left+1000,left+2000)]
        self.scan('original')
        self.vad.detect.side_effect = [[(1000,2000)],RuntimeError('forced interrupted')]
        with self.assertRaises(RuntimeError):self.scan('forced',True)
        self.vad.detect.side_effect = lambda pcm,left: [(left+1000,left+2000)]
        result=self.scan('continue')
        self.assertFalse(result['reused']);self.assertEqual(result['resumed_chunks'],1)

    def test_os_lock_blocks_other_writer_and_releases_on_exit(self):
        folder=self.out/'locktest'
        with ar.check_lock(folder):
            with self.assertRaisesRegex(ValueError,'執行中'):
                with ar.check_lock(folder):pass
        with ar.check_lock(folder):pass


class DraftTests(Fixtures):
    # Reuse fixture setup without rerunning its unrelated tests.
    test_record_readback_and_source_changes_do_not_reuse_marks = None
    test_corrupt_human_record_is_preserved_not_replaced = None
    test_filter_navigation_and_counts_keep_uncertain_pending = None
    test_gui_save_note_and_source_changed_guard = None
    test_speed_label_uses_extracted_clip_and_stop_cancels_end_callback = None

    def test_typing_saves_draft_switch_filter_close_restore_without_confirmation(self):
        p=self.make_panel();p.select_edit(self.row)
        p.decision.set('確實漏句');p.note.set('尚未確認的問候語')
        self.assertEqual(ar.read_draft(self.hashes,self.row)['note'],'尚未確認的問候語')
        self.assertFalse(ar.decision_file(self.hashes,self.row).exists())
        p.select_edit(p.rows[2]);p.draw_rows();self.assertIn('已判定 0',p.review_count.get())
        p.close();self.window.destroy();self.window=None
        p=self.make_panel();p.select_edit(self.row)
        self.assertEqual(p.note.get(),'尚未確認的問候語')
        self.assertIn('尚未確認',p.draft_status.get())
        self.assertIsNone(ar.read_draft(dict(self.hashes,media='f'*64),self.row))

    def test_explicit_save_confirms_and_clears_draft(self):
        p=self.make_panel()
        with patch.object(p,'play_selected'):
            p.tree.selection_set('1');self.window.update()
            p.select_edit(self.row);p.decision.set('沒有漏句');p.note.set('確認是停頓')
            p.save_mark();self.window.update()
        self.assertEqual(ar.read_decision(self.hashes,self.row)['note'],'確認是停頓')
        self.assertFalse(ar.draft_file(self.hashes,self.row).exists())
        self.assertIn('已判定 1',p.review_count.get())

    def test_write_failure_blocks_close_filter_and_row_switch_preserving_input(self):
        p=self.make_panel();p.select_edit(self.row)
        with patch('audio_review_panel.write_draft',side_effect=OSError('disk full')):
            p.note.set('不能丟掉')
            self.assertFalse(p.close());self.assertFalse(p.draw_rows())
            self.assertFalse(p.select_edit(p.rows[2]))
            self.assertEqual(p.note.get(),'不能丟掉')
            self.assertEqual(p.edit_row,self.row)
        self.assertTrue(p.flush_draft())

    def test_corrupt_draft_preserved_and_new_formal_record_wins_over_stale_draft(self):
        p=self.make_panel();p.select_edit(self.row);p.note.set('舊草稿')
        ar.write_decision(self.hashes,self.row,'沒有漏句','較新的正式核對')
        self.assertIsNone(ar.read_draft(self.hashes,self.row))
        path=ar.draft_file(self.hashes,self.row);path.write_text('{broken',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'草稿紀錄無效'):
            ar.write_draft(self.hashes,self.row,'確實漏句','new',p.edit_base)
        self.assertEqual(path.read_text(),'{broken')
        # Restore valid input so fixture teardown can close normally.
        path.unlink();p.edit_row=None

    def test_wizard_navigation_and_close_abort_when_draft_cannot_save(self):
        from launch_menu import WizardMenu
        import tkinter as tk
        self.window=tk.Tk();self.window.withdraw()
        config={'desktop_input_dir':str(self.root),'desktop_output_dir':str(self.root),'workspace_dir':str(self.root)}
        menu=WizardMenu(self.window,config);self.panel=menu.audio_review
        menu.mode.set('gapcheck');menu.show_page('audio_review')
        with patch.object(menu.audio_review,'close',return_value=False):
            menu.show_page('home');self.assertEqual(menu.page,'audio_review')
            menu.cancel();self.assertTrue(self.window.winfo_exists())


del Fixtures
if __name__=='__main__':unittest.main()
