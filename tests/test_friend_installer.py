import json,sys,tempfile,unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import build_friend_installer as installer

class FriendInstallerTests(unittest.TestCase):
    def test_trailer_round_trip_keeps_payload_identity(self):
        metadata={'payload':'影音轉文字朋友版_完整離線環境_8B_x.zip','bytes':8175141283,'sha256':'a'*64}
        with tempfile.TemporaryDirectory() as temp:
            exe=Path(temp)/'setup.exe';exe.write_bytes(b'MZ'+b'\0'*500+installer.trailer(metadata))
            self.assertEqual(installer.read_trailer(exe),metadata)

    def test_missing_or_oversized_trailer_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            plain=Path(temp)/'plain.exe';plain.write_bytes(b'MZ'+b'\0'*100)
            with self.assertRaises(ValueError):installer.read_trailer(plain)
            huge=Path(temp)/'huge.exe';huge.write_bytes(b'MZ'+b'\0'*100+(5000).to_bytes(8,'little')+installer.MAGIC)
            with self.assertRaises(ValueError):installer.read_trailer(huge)

    def test_installer_never_requests_admin_and_payload_is_hash_checked(self):
        manifest=(ROOT/'packaging/installer/app.manifest').read_text(encoding='utf-8')
        self.assertIn('level="asInvoker"',manifest)
        source=(ROOT/'packaging/installer/FriendInstaller.cs').read_text(encoding='utf-8')
        for guard in ('!= PayloadSha256','--prepare-only','FAT32','GetFileSystemEntries(target).Length > 0','IsInstalled()','AddRange(have)'):
            self.assertIn(guard,source)

    def test_drive_share_link_becomes_direct_download_and_view_page(self):
        url,page=installer.drive_urls('https://drive.google.com/file/d/AbC_12-x/view?usp=sharing')
        self.assertEqual(url,'https://drive.usercontent.google.com/download?id=AbC_12-x&export=download&confirm=t')
        self.assertEqual(page,'https://drive.google.com/file/d/AbC_12-x/view')
        self.assertEqual(installer.drive_urls('https://drive.google.com/open?id=Z9')[0],'https://drive.usercontent.google.com/download?id=Z9&export=download&confirm=t')
        self.assertEqual(installer.drive_urls('http://127.0.0.1:1/share'),('http://127.0.0.1:1/share','http://127.0.0.1:1/share'))

    def test_metadata_pins_package_manifest_and_rejects_unsafe_url(self):
        import zipfile
        with tempfile.TemporaryDirectory() as temp:
            archive=Path(temp)/'p.zip'
            with zipfile.ZipFile(archive,'w') as z:z.writestr(installer.PACKAGE_MANIFEST,'{"offline": true}')
            meta=installer.payload_metadata(archive,'b'*64,'https://x/y?id=1','https://x/view')
            self.assertEqual(meta['manifest_sha256'],__import__('hashlib').sha256(b'{"offline": true}').hexdigest())
            self.assertEqual((meta['url'],meta['page'],meta['bytes']),('https://x/y?id=1','https://x/view',archive.stat().st_size))
            with self.assertRaises(ValueError):installer.payload_metadata(archive,'b'*64,'https://x/"y')

    def test_installed_desktop_folders_drive_input_and_output(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('friend_start_folders',ROOT/'packaging/friend_start.py')
        bootstrap=importlib.util.module_from_spec(spec);spec.loader.exec_module(bootstrap)
        with tempfile.TemporaryDirectory() as temp:
            system=Path(temp)/'影音轉文字朋友版/system';system.mkdir(parents=True)
            self.assertEqual(bootstrap.user_folders(system),(system.parent/'音檔',system.parent/'文字檔'))
            desktop=Path(temp)/'Desktop'
            (system/'user_folders.json').write_text(json.dumps({'input_dir':str(desktop/'音檔'),'output_dir':str(desktop/'文字檔')}),encoding='utf-8')
            self.assertEqual(bootstrap.user_folders(system),(desktop/'音檔',desktop/'文字檔'))
            (system/'user_folders.json').write_text(json.dumps({'input_dir':'音檔','output_dir':str(desktop/'文字檔')}),encoding='utf-8')
            with self.assertRaises(RuntimeError):bootstrap.user_folders(system)

    def test_installer_creates_desktop_folders_without_touching_existing_ones(self):
        source=(ROOT/'packaging/installer/FriendInstaller.cs').read_text(encoding='utf-8')
        for guard in ('user_folders.json','沿用桌面既有資料夾','createdFolders','name == "音檔/"','"--desktop-dir"'):
            self.assertIn(guard,source)

if __name__=='__main__':unittest.main()
