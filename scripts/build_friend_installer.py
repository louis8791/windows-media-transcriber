"""Make the friend installer folder: a small installer EXE plus the hash-pinned offline ZIP.

Windows will not start an EXE larger than 4 GB, so the ~8 GB package cannot be appended to
the EXE. The EXE carries the ZIP's name, size and SHA-256, the package manifest SHA-256 (to
recognise an existing install) and an optional download URL in a trailer instead.
"""
from pathlib import Path
import argparse,hashlib,io,json,os,re,struct,subprocess,tempfile,zipfile

ROOT=Path(__file__).resolve().parents[1]
MAGIC=b'YYZWPKG1'
SOURCE=ROOT/'packaging/installer/FriendInstaller.cs';MANIFEST=ROOT/'packaging/installer/app.manifest'
INSTALLER_NAME='安裝影音轉文字朋友版.exe'
PACKAGE_MANIFEST='影音轉文字朋友版/system/package-manifest.json'
def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
def trailer(metadata):
    data=json.dumps(metadata,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    return data+struct.pack('<q',len(data))+MAGIC
def read_trailer(path):
    with Path(path).open('rb') as stream:
        stream.seek(-16,os.SEEK_END);tail=stream.read(16)
        if tail[8:]!=MAGIC:raise ValueError('installer trailer missing')
        size=struct.unpack('<q',tail[:8])[0]
        if not 0<size<=4096:raise ValueError('installer trailer invalid')
        stream.seek(-16-size,os.SEEK_END);return json.loads(stream.read(size).decode('utf-8'))
def drive_urls(link):
    """Google Drive share link -> (direct download URL, human download page)."""
    match=re.search(r'/file/d/([A-Za-z0-9_-]+)',link) or re.search(r'[?&]id=([A-Za-z0-9_-]+)',link)
    if not match:return link,link
    file_id=match.group(1)
    return (f'https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t',
            f'https://drive.google.com/file/d/{file_id}/view')
def payload_metadata(zip_path,sha256,url='',page=''):
    with zipfile.ZipFile(zip_path) as archive:manifest=hashlib.sha256(archive.read(PACKAGE_MANIFEST)).hexdigest()
    for text in (url,page):
        if '"' in text or '\\' in text:raise ValueError('URL must not contain quotes or backslashes')
    return {'payload':Path(zip_path).name,'bytes':Path(zip_path).stat().st_size,'sha256':sha256,'manifest_sha256':manifest,'url':url,'page':page}
def compile_stub(target):
    framework=Path(os.environ['SystemRoot'])/'Microsoft.NET/Framework64/v4.0.30319'
    references=[framework/name for name in ('System.IO.Compression.dll','System.Windows.Forms.dll','System.Drawing.dll')]
    subprocess.run([str(framework/'csc.exe'),'/nologo','/target:winexe','/codepage:65001','/optimize+','/platform:anycpu',
        '/win32manifest:'+str(MANIFEST),*('/r:'+str(r) for r in references),'/out:'+str(target),str(SOURCE)],
        check=True,creationflags=subprocess.CREATE_NO_WINDOW)
def upgrade_patch(old_zip,new_zip):
    """Files of new_zip whose content differs from old_zip, as a small ZIP the EXE carries so an
    install of old_zip can be updated in place. Removed files are not expected and are refused."""
    with zipfile.ZipFile(old_zip) as old,zipfile.ZipFile(new_zip) as new:
        before=json.loads(old.read(PACKAGE_MANIFEST))['files'];after=json.loads(new.read(PACKAGE_MANIFEST))['files']
        if set(before)-set(after):raise ValueError('upgrade would need to remove files: %s'%sorted(set(before)-set(after)))
        changed=sorted(name for name,digest in after.items() if before.get(name)!=digest)
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as patch:
            for name in [*changed,'system/package-manifest.json']:patch.writestr('影音轉文字朋友版/'+name,new.read('影音轉文字朋友版/'+name))
        old_manifest=hashlib.sha256(old.read(PACKAGE_MANIFEST)).hexdigest()
    data=buffer.getvalue()
    return data,{'upgrade_from':old_manifest,'patch_bytes':len(data),'patch_sha256':hashlib.sha256(data).hexdigest()},changed
def write_installer(target,metadata,patch=b''):
    with tempfile.TemporaryDirectory(dir=ROOT/'work') as temp:
        stub=Path(temp)/'FriendInstaller.exe';compile_stub(stub)
        Path(target).write_bytes(stub.read_bytes()+patch+trailer(metadata))
    assert read_trailer(target)==metadata
def build(zip_path,folder_name='安裝包',url='',upgrade_from=None):
    zip_path=Path(zip_path).resolve();output=zip_path.parent
    receipt=json.loads((output/'build.json').read_text(encoding='utf-8'))
    assert Path(receipt['zip']).resolve()==zip_path and sha(zip_path)==receipt['sha256'],'ZIP does not match its build receipt'
    folder=output/folder_name;folder.mkdir()
    payload=folder/zip_path.name;os.link(zip_path,payload)
    url,page=drive_urls(url) if url else ('','')
    metadata=payload_metadata(payload,receipt['sha256'],url,page);patch=b'';changed=[]
    if upgrade_from:
        patch,extra,changed=upgrade_patch(upgrade_from,payload);metadata.update(extra)
    write_installer(folder/INSTALLER_NAME,metadata,patch)
    result={'folder':str(folder),'installer':str(folder/INSTALLER_NAME),'installer_bytes':(folder/INSTALLER_NAME).stat().st_size,
        'installer_sha256':sha(folder/INSTALLER_NAME),'installer_source_sha256':sha(SOURCE),'payload':str(payload),
        'payload_bytes':metadata['bytes'],'payload_sha256':metadata['sha256'],'manifest_sha256':metadata['manifest_sha256'],'url':url,'page':page,'offline':not url,
        'upgrade_from':metadata.get('upgrade_from',''),'upgrade_changed_files':changed}
    (output/'installer_build.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False),flush=True);return folder
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('zip',type=Path);parser.add_argument('--folder-name',default='安裝包')
    parser.add_argument('--url',default='',help='optional Google Drive share link or direct URL, used only when no local ZIP is found')
    parser.add_argument('--upgrade-from',type=Path,help='previous release ZIP; installs of it are updated in place')
    args=parser.parse_args();build(args.zip,args.folder_name,args.url,args.upgrade_from)
