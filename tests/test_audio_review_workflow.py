import json
from pathlib import Path
import sys,tempfile,time,wave,unittest
from unittest.mock import patch
import tkinter as tk

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from audio_review import sha,read_decision,write_decision,decision_file
from audio_review_panel import AudioReviewPanel


class AudioReviewWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'test_artifacts');self.root=Path(self.temp.name)
        self.media=self.root/'a.wav';self.media.write_bytes(b'original source')
        self.transcript=self.root/'a.srt';self.transcript.write_text('1\n00:00:01,000 --> 00:00:02,000\nHello.\n',encoding='utf-8')
        self.hashes={'media':sha(self.media),'transcript':sha(self.transcript)}
        self.row={'start_ms':3000,'end_ms':4000,'text':'review this','kind':'possible_omission'}
        self.root_patch=patch('audio_review.ROOT',self.root);self.root_patch.start()
        self.window=None
    def tearDown(self):
        if self.window:
            self.panel.close();self.window.update();self.window.destroy()
        self.root_patch.stop();self.temp.cleanup()
    def make_panel(self):
        self.window=tk.Tk();self.window.withdraw()
        self.panel=p=AudioReviewPanel(self.window,{})
        p.media,p.transcript,p.folder=self.media,self.transcript,self.root
        p.duration_ms=8000
        p.cues=[{'start_ms':1000,'end_ms':2000,'text':'Hello.'}]
        p.review_hashes=self.hashes;p.source_stats=p.file_stats()
        p.show_rows([self.row,dict(self.row,start_ms=5000,end_ms=6000)])
        self.window.update();return p
    def test_record_readback_and_source_changes_do_not_reuse_marks(self):
        write_decision(self.hashes,self.row,'沒有漏句','掌聲後有停頓')
        self.assertEqual(read_decision(self.hashes,self.row)['note'],'掌聲後有停頓')
        changed=dict(self.hashes,media='f'*64)
        self.assertEqual(read_decision(changed,self.row)['decision'],'尚未核對')
        changed=dict(self.hashes,transcript='f'*64)
        self.assertEqual(read_decision(changed,self.row)['decision'],'尚未核對')
    def test_corrupt_human_record_is_preserved_not_replaced(self):
        path=decision_file(self.hashes,self.row);path.parent.mkdir(parents=True);path.write_text('{broken',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'核對紀錄無效'):write_decision(self.hashes,self.row,'確實漏句','note')
        self.assertEqual(path.read_text(encoding='utf-8'),'{broken')
    def test_filter_navigation_and_counts_keep_uncertain_pending(self):
        p=self.make_panel();p.only_findings.set(True);p.draw_rows()
        self.assertEqual(p.tree.get_children(),('1','2'))
        with patch.object(p,'play_selected'):
            p.jump_finding(1);self.window.update();self.assertEqual(p.tree.selection(),('1',))
            p.jump_finding(1);self.window.update();self.assertEqual(p.tree.selection(),('2',))
            p.jump_finding(1);self.window.update();self.assertEqual(p.tree.selection(),('1',))
            p.jump_finding(-1);self.window.update();self.assertEqual(p.tree.selection(),('2',))
        p.decisions[(3000,4000)]={'decision':'沒有漏句','note':''}
        p.decisions[(5000,6000)]={'decision':'仍不確定','note':''}
        p.only_pending.set(True);p.draw_rows()
        self.assertEqual(p.tree.get_children(),('2',));self.assertIn('已判定 1',p.review_count.get())
        self.assertIn('待核對 1',p.review_count.get())
    def test_gui_save_note_and_source_changed_guard(self):
        p=self.make_panel()
        with patch.object(p,'play_selected'):
            p.tree.selection_set('1');self.window.update()
            p.decision.set('確實漏句');p.note.set('這句缺少問候語');p.save_mark()
        self.assertEqual(read_decision(self.hashes,self.row)['note'],'這句缺少問候語')
        before=decision_file(self.hashes,self.row).read_bytes()
        self.media.write_bytes(b'changed audio')
        with patch.object(p,'play_selected'):
            p.tree.selection_set('1');self.window.update();p.decision.set('沒有漏句');p.save_mark()
        self.assertIn('未保存',p.status.get())
        self.assertEqual(decision_file(self.hashes,self.row).read_bytes(),before)
    def test_speed_label_uses_extracted_clip_and_stop_cancels_end_callback(self):
        p=self.make_panel()
        with patch.object(p,'play_selected'):
            p.tree.selection_set('0');self.window.update()
        class Process:
            returncode=0
            def poll(self):return 0
        def create(command,**_):
            with wave.open(command[-1],'wb') as f:
                f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(bytes(16000))
            return Process()
        with patch('audio_review_panel.ffmpeg',return_value='ffmpeg.exe'),patch('audio_review_panel.subprocess.Popen',side_effect=create),patch('audio_review_panel.winsound.PlaySound'):
            p.speed.set('原速');p.play_selected();p.speed.set('0.5倍')
            deadline=time.monotonic()+1
            while not p.status.get().startswith('播放：') and time.monotonic()<deadline:self.window.update();time.sleep(.01)
            self.assertIn('原速',p.status.get());self.assertNotIn('0.5倍',p.status.get())
            p.stop_playback(True);self.assertEqual(p.status.get(),'播放已停止。')
            deadline=time.monotonic()+.6
            while time.monotonic()<deadline:self.window.update();time.sleep(.01)
            self.assertEqual(p.status.get(),'播放已停止。')


if __name__=='__main__':unittest.main()
