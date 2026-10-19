import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from whisper_learning import validate, compare, sha, save_new
from train_whisper import train

class LearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'test_artifacts')
        self.folder = Path(self.temp.name)
        self.data = {'records':[]}
        for i,split in enumerate(('train','validation','test')):
            audio = self.folder/f'{i}.wav'
            with wave.open(str(audio),'wb') as f:
                f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(bytes([i,0])*16000)
            self.data['records'].append(dict(id=str(i),source_path=str(audio),source_sha256=sha(audio),audio_path=str(audio),audio_sha256=sha(audio),start_ms=0,end_ms=1000,text='We need justice not war.',language='en',split=split,approved=True,verified_by='test fixture only',verified_at='2026-10-04'))
    def tearDown(self):self.temp.cleanup()
    def test_unreviewed_training_stops_before_import_or_output(self):
        self.data['records'][0]['approved']=False
        path=self.folder/'dataset.json';save_new(path,self.data)
        with self.assertRaisesRegex(ValueError,'人工聽校'):train(path,self.folder/'missing',self.folder/'run')
        self.assertFalse((self.folder/'run').exists())
    def test_same_recording_cannot_leak_between_splits(self):
        other=self.data['records'][1];source=self.data['records'][0]
        other.update(source_path=source['source_path'],source_sha256=source['source_sha256'])
        with self.assertRaisesRegex(ValueError,'同一錄音'):validate(self.data)
    def test_changed_clip_rejected(self):
        Path(self.data['records'][0]['audio_path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'hash'):validate(self.data)
    def test_compare_requires_complete_ids_and_no_automatic_promotion(self):
        with self.assertRaisesRegex(ValueError,'完整'):compare(self.data,{}, {'2':'We need justice not war.'})
        result=compare(self.data,{'2':'We need justice war.'},{'2':'We need justice not war.'})
        self.assertEqual(result['status'],'candidate_improved');self.assertFalse(result['production_promoted'])
        result=compare(self.data,{'2':'We need justice not war.'},{'2':'We need justice war.'})
        self.assertEqual(result['status'],'not_accepted')
    def test_real_menu_preflight_explains_missing_data_without_training(self):
        import tkinter as tk
        import whisper_learning_menu
        seen=[]
        def check_window(window):
            window.update()
            for parent in window.winfo_children():
                for button in parent.winfo_children():
                    if button.winfo_class()=='TButton' and button.cget('text')=='檢查能否訓練':button.invoke()
            texts=[window.getvar(str(w.cget('textvariable'))) for w in window.winfo_children() if w.winfo_class()=='TLabel' and w.cget('textvariable')]
            self.assertTrue(any('尚無教材' in text and '尚未訓練' in text for text in texts))
            seen.append(True);window.destroy()
        with patch.object(tk.Tk,'mainloop',check_window):whisper_learning_menu.main()
        self.assertTrue(seen)

if __name__=='__main__':unittest.main()
