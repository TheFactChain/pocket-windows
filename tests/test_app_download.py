"""Selective updater verifies every file and never downloads unchanged payloads."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT/'scripts/app_download.py'
downloader = None
if SCRIPT.exists():
    spec = importlib.util.spec_from_file_location('app_download', SCRIPT)
    downloader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(downloader)

class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(downloader, 'Selective downloader is missing')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.target=self.base/'installed';self.target.mkdir()
        self.output=self.base/'app.zip'
        self.files={'release-info.json':b'{"version":"new"}','scripts/portable_app.py':b'new backend',
                    'runtime/python-3.12.14/unchanged.bin':b'x'*131072}
        archive=io.BytesIO()
        rows=[];entries=[]
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as bundle:
            for name,data in self.files.items():bundle.writestr('app/'+name,data)
        self.raw=archive.getvalue()
        with zipfile.ZipFile(io.BytesIO(self.raw)) as bundle:
            for name,data in self.files.items():
                entry=bundle.getinfo('app/'+name)
                offset=entry.header_offset
                start=offset+30+int.from_bytes(self.raw[offset+26:offset+28],'little')+int.from_bytes(self.raw[offset+28:offset+30],'little')
                rows.append({'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
                entries.append({'path':name,'offset':start,'compressed_bytes':entry.compress_size,'method':entry.compress_type})
        self.index={'schema':1,'release':{'schema':1,'release_id':'2026.10.26.2','source_commit':'a'*40,'artifacts':{},'common_files':rows},
                    'archive':{'url':'https://github.com/TheFactChain/pocket-windows/releases/download/v2026.10.26.2/Pocket-Windows-2026.10.26.2-app.zip','bytes':len(self.raw),'sha256':hashlib.sha256(self.raw).hexdigest()},'files':entries}
        self.requests=[];self.corrupt=False;self.status=206
        self.save_index()

    def save_index(self):
        self.path=self.base/'index.json';self.path.write_bytes(json.dumps(self.index).encode())
        self.digest=hashlib.sha256(self.path.read_bytes()).hexdigest()

    def response(self,request,timeout):
        start,end=map(int,request.get_header('Range').removeprefix('bytes=').split('-'))
        self.requests.append((start,end))
        payload=self.raw[start:end+1]
        if self.corrupt:payload=b'!'*len(payload)
        response=io.BytesIO(payload)
        response.status=self.status;response.url=request.full_url
        response.headers={'Content-Range':f'bytes {start}-{end}/{len(self.raw)}','Content-Length':str(len(payload))}
        return response

    def run_download(self):
        with mock.patch.object(downloader.urllib.request,'urlopen',side_effect=self.response):
            return downloader.download(self.path,self.digest,self.target,self.output)

    def test_only_changed_file_ranges_are_downloaded(self):
        for name,data in self.files.items():
            path=self.target/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        (self.target/'scripts/portable_app.py').write_bytes(b'old backend')
        result=self.run_download()
        expected=next(entry['compressed_bytes'] for entry in self.index['files'] if entry['path']=='scripts/portable_app.py')
        self.assertEqual(result['downloaded_bytes'],expected)
        self.assertEqual(len(self.requests),1)
        with zipfile.ZipFile(self.output) as bundle:
            manifest=json.loads(bundle.read('RELEASE.json'))
            self.assertIn('runtime/python-3.12.14/unchanged.bin',manifest['omitted'])
            self.assertEqual(bundle.read('app/scripts/portable_app.py'),b'new backend')
            self.assertIn('app/release-info.json',bundle.namelist())

    def test_missing_and_corrupted_files_are_retrieved(self):
        path=self.target/'scripts/portable_app.py';path.parent.mkdir(parents=True);path.write_bytes(b'corrupt')
        result=self.run_download()
        self.assertEqual(result['changed_files'],len(self.files))
        self.assertEqual(len(self.requests),len(self.files))

    def test_ignored_range_and_corrupt_data_leave_no_package(self):
        self.status=200
        with self.assertRaises(ValueError):self.run_download()
        self.assertFalse(self.output.exists())
        self.status=206;self.corrupt=True
        with self.assertRaises((ValueError,zlib.error)):self.run_download()
        self.assertFalse(self.output.exists())

    def test_bad_index_hash_is_rejected_before_network(self):
        self.path.write_bytes(self.path.read_bytes()+b' ')
        with self.assertRaises(ValueError):self.run_download()
        self.assertEqual(self.requests,[])

    def test_existing_output_is_preserved(self):
        self.output.write_bytes(b'keep existing file')
        with self.assertRaises(ValueError):self.run_download()
        self.assertEqual(self.output.read_bytes(),b'keep existing file')
        self.assertEqual(self.requests,[])

    def test_paths_urls_overlaps_and_types_are_rejected(self):
        for change in ('path','url','overlap','boolean'):
            previous=json.loads(json.dumps(self.index))
            if change=='path':self.index['files'][0]['path']='../private.txt'
            elif change=='url':self.index['archive']['url']='https://example.com/app.zip'
            elif change=='overlap':self.index['files'][1]['offset']=self.index['files'][0]['offset']
            else:self.index['files'][0]['offset']=True
            self.save_index()
            with self.subTest(change=change),self.assertRaises(ValueError):self.run_download()
            self.assertEqual(self.requests,[])
            self.index=previous

if __name__=='__main__':unittest.main()
