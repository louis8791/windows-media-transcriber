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
from whisper_learning import validate, compare, sha, save_new, score_predictions, acceptable_score, ValidationProgress
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
    def test_owner_weak_training_requires_explicit_flag_and_cannot_enter_test(self):
        row=self.data['records'][0]
        row.update(approved=False,verified_by='',verified_at='',annotation_type='owner_authorized_asr_candidate',target_authorized_by='Owner',target_authorized_at='2026-10-04')
        self.data['weak_supervision_authorized']=True
        with self.assertRaisesRegex(ValueError,'人工聽校'):validate(self.data)
        self.assertEqual(len(validate(self.data,allow_weak_training=True)),3)
        other=self.data['records'][2]
        other.update(approved=False,annotation_type=row['annotation_type'],target_authorized_by='Owner',target_authorized_at='2026-10-04')
        with self.assertRaisesRegex(ValueError,'人工聽校'):validate(self.data,allow_weak_training=True)
    def test_invalid_learning_rate_stops_before_model_import_and_output(self):
        path=self.folder/'dataset.json';save_new(path,self.data)
        with patch('train_whisper.preflight',return_value={'ready':True}):
            for rate in (0,-0.1,0.002,float('nan')):
                with self.subTest(rate=rate),self.assertRaisesRegex(ValueError,'學習率'):
                    train(path,self.folder/'missing',self.folder/'run',learning_rate=rate)
        self.assertFalse((self.folder/'run').exists())
    def test_changed_clip_rejected(self):
        Path(self.data['records'][0]['audio_path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'hash'):validate(self.data)
    def test_speaker_cannot_leak_even_with_different_recordings(self):
        self.data['records'][0]['speaker_id']='same'
        self.data['records'][2]['speaker_id']='same'
        with self.assertRaisesRegex(ValueError,'同一講者'):validate(self.data)
    def test_generated_score_detects_omissions_and_insertions(self):
        rows=[self.data['records'][2]]
        score=score_predictions(rows,{'2':'We need justice war.'})
        self.assertEqual(score['errors'],1)
        self.assertEqual(score['deletions'],1)
        score=score_predictions(rows,{'2':'We need justice not war today.'})
        self.assertEqual(score['errors'],1)
        self.assertEqual(score['insertions'],1)
    def test_aggregate_improvement_cannot_hide_speaker_regression(self):
        rows=[dict(self.data['records'][2],speaker_id='A'),dict(self.data['records'][2],id='3',speaker_id='B')]
        baseline=score_predictions(rows,{'2':'We need justice not war.','3':'hello'})
        candidate=score_predictions(rows,{'2':'We need justice war.','3':'We need justice not war.'})
        self.assertLess(candidate['errors'],baseline['errors'])
        self.assertFalse(acceptable_score(baseline,candidate))
    def test_validation_stops_on_real_regression_or_stagnation(self):
        baseline={'errors':10,'error_rate':0.1}
        progress=ValidationProgress(baseline)
        self.assertIsNone(progress.observe({'errors':11,'error_rate':0.11}))
        self.assertEqual(progress.observe({'errors':12,'error_rate':0.12}),'two_consecutive_validation_regressions')
        progress=ValidationProgress(baseline)
        self.assertIsNone(progress.observe({'errors':9,'error_rate':0.09}))
        for _ in range(2):self.assertIsNone(progress.observe({'errors':9,'error_rate':0.09}))
        self.assertEqual(progress.observe({'errors':9,'error_rate':0.09}),'three_checks_without_validation_improvement')
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
