import io, json, os, tempfile, time, unittest, urllib.error
from pathlib import Path
from unittest.mock import patch
import uploader as u

class Response(io.BytesIO):
    def __init__(self,obj):
        super().__init__(json.dumps(obj).encode()); self.headers={}

class Tests(unittest.TestCase):
    def setUp(self):
        p=patch.dict(os.environ,TG_TOKEN='fake',CHAT_ID='-100123'); p.start(); self.addCleanup(p.stop)
        self.e=dict(path='data/001.PMC1/a.pdf',sha='abc',caption='test')
    def test_allocation(self):
        for n,v in [(1,'fzm'),(6164,'fzm'),(6165,'ajk'),(12012,'ajk'),(12013,'mas'),(17937,'mas'),(17938,'kf'),(23968,'kf'),(23969,'unassigned')]: self.assertEqual(u.reviewer(n),v)
    def test_sequences(self):
        es=u.inventory([dict(path='data/001.PMC1/b.pdf',sha='b'),dict(path='data/001.PMC1/a.pdf',sha='a'),dict(path='data/no.txt',sha='x')])
        self.assertEqual(len(es),2)
        remaining=[e for e in es if u.pending(e,{}, {es[0]['path']})]
        self.assertIn('Sequence number: 2/2',remaining[0]['caption'])
    def test_resume(self):
        for status in ['sent','skipped','uncertain','inflight']:
            self.assertFalse(u.pending(self.e,{self.e['path']:dict(self.e,status=status)},set()))
        self.assertTrue(u.pending(self.e,{self.e['path']:dict(self.e,status='failed')},set()))
        self.assertTrue(u.pending(self.e,{self.e['path']:dict(self.e,sha='old',status='sent')},set()))
        self.assertFalse(u.pending(self.e,{self.e['path']:dict(self.e,sha='old',status='uncertain')},set()))
        self.assertFalse(u.pending(self.e,{}, {self.e['path']}))
    def test_journal(self):
        with tempfile.TemporaryDirectory() as t, patch.object(u,'STATE',Path(t)):
            u.record(self.e,'inflight'); u.record(self.e,'sent',message_id=5)
            self.assertEqual(u.load_state()[self.e['path']]['message_id'],5)
            with (Path(t)/'uploads.jsonl').open('a') as f: f.write('{bad')
            with self.assertRaises(json.JSONDecodeError): u.load_state()
    def test_success(self):
        with patch.object(u.urllib.request,'urlopen',return_value=Response({'ok':True,'result':{'message_id':3,'document':{'file_id':'x'}}})):
            status,details=u.send(self.e,b'data',time.monotonic()+1000)
        self.assertEqual(status,'sent'); self.assertEqual(details['message_id'],3)
    def test_lost_response(self):
        with patch.object(u.urllib.request,'urlopen',side_effect=TimeoutError) as request:
            self.assertEqual(u.send(self.e,b'data',time.monotonic()+1000)[0],'uncertain'); self.assertEqual(request.call_count,1)
    def test_rate_limit(self):
        error=urllib.error.HTTPError('test',429,'limited',{},io.BytesIO(json.dumps({'ok':False,'error_code':429,'parameters':{'retry_after':4}}).encode()))
        with patch.object(u.urllib.request,'urlopen',side_effect=[error,Response({'ok':True,'result':{'message_id':3}})]),patch.object(u.time,'sleep') as sleep:
            self.assertEqual(u.send(self.e,b'data',time.monotonic()+1000)[0],'sent'); sleep.assert_called_once_with(5)
    def test_server_error(self):
        with patch.object(u.urllib.request,'urlopen',return_value=Response({'ok':False,'error_code':500})) as request:
            self.assertEqual(u.send(self.e,b'data',time.monotonic()+1000)[0],'uncertain'); self.assertEqual(request.call_count,1)
    def test_budget(self):
        with patch.object(u.urllib.request,'urlopen') as request:
            self.assertEqual(u.send(self.e,b'data',time.monotonic()+10)[0],'failed'); request.assert_not_called()
    def test_large_download(self):
        r=io.BytesIO(b'abcdef'); r.headers={}
        with patch.object(u.urllib.request,'urlopen',return_value=r):
            with self.assertRaises(OverflowError): u.fetch('a/b','c','file',limit=5)
    def test_hash(self): self.assertEqual(u.blob_sha(b''),'e69de29bb2d1d6434b8b29ae775ad8c2e48c5391')
    def test_multipart(self):
        boundary,body=u.multipart('123','caption','a"\r\n.pdf',b'content')
        self.assertIn(b'filename="a___.pdf"',body)
        self.assertTrue(body.endswith(('--'+boundary+'--\r\n').encode()))
