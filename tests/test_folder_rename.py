import json
from pathlib import Path
import sys,tempfile,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import migrate_desktop_folders as migration

class RenameTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'test_artifacts');self.root=Path(self.temp.name)
        self.desktop=self.root/'desktop';self.desktop.mkdir()
        self.pairs=[(self.desktop/'廣播音檔',self.desktop/'音檔'),(self.desktop/'廣播文字檔',self.desktop/'文字檔')]
        for old,_ in self.pairs:old.mkdir()
        self.audio=self.pairs[0][0]/'sample.wav';self.audio.write_bytes(b'unchanged-audio-fixture')
        (self.pairs[1][0]/'renamed.txt').write_text('preserve edited transcript',encoding='utf-8')
        (self.root/'config.local.json').write_text(json.dumps({'desktop_input_dir':str(self.pairs[0][0]),'desktop_output_dir':str(self.pairs[1][0])}),encoding='utf-8')
        self.patchers=[patch.object(migration,'ROOT',self.root),patch.object(migration,'DESKTOP',self.desktop),patch.object(migration,'PAIRS',self.pairs)]
        for p in self.patchers:p.start()
    def tearDown(self):
        for p in reversed(self.patchers):p.stop()
        self.temp.cleanup()
    def test_real_rename_preserves_data_job_id_and_completion(self):
        old_id=migration.job_id(self.audio,self.audio.stat())
        receipts=self.root/'logs/completed_media';receipts.mkdir(parents=True)
        receipt=receipts/'receipt.json';receipt.write_text(json.dumps({'media_sha256':migration.digest(self.audio),'input_path':str(self.audio),'input_paths':[str(self.audio)],'completed_at':'preserve-original-date'}),encoding='utf-8')
        migration.main()
        config=json.loads((self.root/'config.local.json').read_text(encoding='utf-8'))
        moved=self.pairs[0][1]/'sample.wav'
        self.assertEqual(migration.job_id(moved,moved.stat(),config['job_identity_input_root'],self.pairs[0][1]),old_id)
        self.assertEqual(moved.read_bytes(),b'unchanged-audio-fixture')
        data=json.loads(receipt.read_text(encoding='utf-8'))
        self.assertIn(str(moved),data['input_paths']);self.assertIn(str(self.audio),data['input_paths'])
        self.assertEqual(data['completed_at'],'preserve-original-date')
        self.assertFalse(self.pairs[0][0].exists())
    def test_bad_material_hash_rolls_back_folders_and_configuration(self):
        original=(self.root/'config.local.json').read_bytes()
        folder=self.root/'work/whisper_learning/pending';folder.mkdir(parents=True)
        dataset=folder/'dataset.json';dataset.write_text(json.dumps({'records':[{'source_path':str(self.audio),'source_sha256':'wrong'}]}),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'教材來源'):migration.main()
        self.assertTrue(self.audio.exists());self.assertFalse(self.pairs[0][1].exists())
        self.assertEqual((self.root/'config.local.json').read_bytes(),original)
    def test_existing_destination_stops_without_touching_originals(self):
        self.pairs[0][1].mkdir()
        with self.assertRaisesRegex(ValueError,'來源、目標'):migration.main()
        self.assertTrue(self.audio.exists())
