import sys,time,wave,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'tests')]
from audio_review import playback_bounds
from test_audio_review_workflow import AudioReviewWorkflowTests as WorkflowFixtures


class PlaybackRangeTests(unittest.TestCase):
    def test_context_clamps_opening_and_ending(self):
        self.assertEqual(playback_bounds({'start_ms':200,'end_ms':1200},8000,'前後3秒'),(0,4200))
        self.assertEqual(playback_bounds({'start_ms':7000,'end_ms':7900},8000,'前後5秒'),(2000,8000))
        self.assertEqual(playback_bounds({'start_ms':7000,'end_ms':7900},8000,'僅所選段'),(7000,7900))
        with self.assertRaises(ValueError):playback_bounds({'start_ms':9000,'end_ms':9500},8000,'前後5秒')


class ReplayCacheTests(unittest.TestCase):
    setUp=WorkflowFixtures.setUp
    tearDown=WorkflowFixtures.tearDown
    make_panel=WorkflowFixtures.make_panel
    def test_replay_reuses_and_speed_range_corrupt_clip_and_source_invalidate(self):
        p=self.make_panel();commands=[]
        with patch.object(p,'play_selected'):
            p.tree.selection_set('0');self.window.update()
        class Process:
            returncode=0
            def poll(self):return 0
        def create(command,**_):
            commands.append(command)
            with wave.open(command[-1],'wb') as f:
                f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(bytes(32000))
            return Process()
        def play():
            p.play_selected();deadline=time.monotonic()+1
            while p.play_process is not None and time.monotonic()<deadline:self.window.update();time.sleep(.01)
            self.assertIsNone(p.play_process)
        with patch('audio_review_panel.ffmpeg',return_value='ffmpeg.exe'),patch('audio_review_panel.subprocess.Popen',side_effect=create),patch('audio_review_panel.winsound.PlaySound'):
            play();self.assertEqual(len(commands),1)
            play();self.assertEqual(len(commands),1);self.assertIn('沿用已擷取片段',p.status.get())
            p.speed.set('0.75倍');play();self.assertEqual(len(commands),2)
            p.context.set('前後3秒');play();self.assertEqual(len(commands),3)
            self.assertEqual(commands[-1][commands[-1].index('-ss')+1],'0.0')
            p.speed.set('原速');p.context.set('前後0.4秒')
            original=next(iter(p.clip_cache.values()))[0];original.write_bytes(b'corrupted')
            play();self.assertEqual(len(commands),4)
            p.media_snapshot=(self.media.stat().st_size,self.media.stat().st_mtime_ns)
            self.media.write_bytes(b'changed source bytes')
            p.play_selected();self.assertEqual(len(commands),4);self.assertIn('原音已更動',p.status.get())
            owned=[path for path,_ in p.clip_cache.values()];p.close()
            self.assertTrue(all(not path.exists() for path in owned))
            self.assertTrue(self.media.exists());self.assertTrue(self.transcript.exists())
    def test_stop_prevents_pending_fragment_from_becoming_replayable(self):
        p=self.make_panel()
        with patch.object(p,'play_selected'):
            p.tree.selection_set('0');self.window.update()
        class Process:
            returncode=None
            def poll(self):return self.returncode
            def terminate(self):self.returncode=1
            def wait(self,timeout=None):return self.returncode
        def create(command,**_):
            Path(command[-1]).write_bytes(b'incomplete')
            return Process()
        with patch('audio_review_panel.ffmpeg',return_value='ffmpeg.exe'),patch('audio_review_panel.subprocess.Popen',side_effect=create),patch('audio_review_panel.winsound.PlaySound'):
            p.play_selected();pending=p.pending_clip;p.stop_playback(True)
            self.window.update();self.assertFalse(pending.exists());self.assertEqual(p.clip_cache,{})

    def test_cache_limit_removes_only_owned_playback_files(self):
        p=self.make_panel();created=[]
        with patch.object(p,'play_selected'):
            p.tree.selection_set('0');self.window.update()
        class Process:
            returncode=0
            def poll(self):return 0
        def create(command,**_):
            path=Path(command[-1]);created.append(path)
            with wave.open(str(path),'wb') as f:
                f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(bytes(3200))
            return Process()
        with patch('audio_review_panel.ffmpeg',return_value='ffmpeg.exe'),patch('audio_review_panel.subprocess.Popen',side_effect=create),patch('audio_review_panel.winsound.PlaySound'):
            for index in range(25):
                p.rows[0]['start_ms']=1000+index*30;p.rows[0]['end_ms']=2000+index*30
                p.play_selected();deadline=time.monotonic()+1
                while p.play_process is not None and time.monotonic()<deadline:self.window.update();time.sleep(.01)
            self.assertEqual(len(p.clip_cache),24)
            self.assertFalse(created[0].exists());self.assertTrue(created[-1].exists())
            self.assertTrue(self.media.exists());self.assertTrue(self.transcript.exists())


del WorkflowFixtures
if __name__=='__main__':unittest.main()
