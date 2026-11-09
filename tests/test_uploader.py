import io, json, os, tempfile, time, types, unittest, urllib.error
from pathlib import Path
from unittest.mock import patch
import uploader as u

class Response(io.BytesIO):
    def __init__(self,obj):
        super().__init__(json.dumps(obj).encode()); self.headers={}

class Tests(unittest.TestCase):
    def setUp(self):
        p=patch.dict(os.environ,TG_TOKEN='fake',CHAT_ID='-100123'); p.start(); self.addCleanup(p.stop)
        # Pin the reviewer used by this test so it behaves consistently in every job;
        # sharding tests below override these explicitly.
        q=patch.object(u,'REVIEWER_KEY','fzm'); q.start(); self.addCleanup(q.stop)
        self.e=dict(path='data/001.PMC1/a.pdf',sha='abc',caption='test')
    def test_allocation(self):
        for n,v in [(1,'fzm'),(6164,'fzm'),(6165,'ajk'),(12012,'ajk'),(12013,'mas'),(17937,'mas'),(17938,'kf'),(23968,'kf'),(23969,'unassigned')]: self.assertEqual(u.reviewer(n),v)
    def test_sequences(self):
        es=u.inventory([dict(path='data/001.PMC1/b.pdf',sha='b'),dict(path='data/001.PMC1/a.pdf',sha='a'),dict(path='data/no.txt',sha='x')])
        self.assertEqual(len(es),2)
        remaining=[e for e in es if u.pending(e,{}, {es[0]['path']})]
        self.assertIn('Sequence number: 2/2',remaining[0]['caption'])
    def test_fzm_must_keep_existing_channel(self):
        meta={'chat_id':'-1003920194277'}
        u.validate_reviewer_destination(meta,'fzm','-1003920194277')
        with self.assertRaisesRegex(ValueError,'CHAT_ID_FZM'):
            u.validate_reviewer_destination(meta,'fzm','-100999')
        # Other reviewers are expected to use their own new destinations.
        u.validate_reviewer_destination(meta,'ajk','-100999')

    def test_resume(self):
        for status in ['sent','skipped','uncertain','inflight']:
            self.assertFalse(u.pending(self.e,{self.e['path']:dict(self.e,status=status,chat_id='-100123')},set()))
        self.assertTrue(u.pending(self.e,{self.e['path']:dict(self.e,status='failed')},set()))
        self.assertTrue(u.pending(self.e,{self.e['path']:dict(self.e,sha='old',status='sent',chat_id='-100123')},set()))
        self.assertFalse(u.pending(self.e,{self.e['path']:dict(self.e,sha='old',status='uncertain',chat_id='-100123')},set()))
        self.assertTrue(u.pending(self.e,{self.e['path']:dict(self.e,status='sent',chat_id='-100999')},set(),chat='-100123'))
        self.assertFalse(u.pending(self.e,{}, {self.e['path']},chat='-100123',legacy_chat='-100123'))
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

    # --- parallel bots (sharding) ---
    def test_unsharded_owns_everything(self):
        self.assertEqual(u.REVIEWER_KEY,'fzm')
        self.assertTrue(u.mine(self.e))
    def test_reviewer_partition(self):
        numbers=[1,6164,6165,12012,12013,17937,17938,23968]
        es=u.inventory([dict(path=f'data/{n:05d}.PMC{n}/{c}.pdf',sha=f'{n}{c}') for n in numbers for c in 'ab'])
        owners=[]
        for key in ['fzm','ajk','mas','kf']:
            with patch.object(u,'REVIEWER_KEY',key):
                owners.append({e['path'] for e in es if u.mine(e)})
        self.assertEqual(set().union(*owners),{e['path'] for e in es})
        self.assertEqual(sum(map(len,owners)),len(es))
        articles=[{p.split('/')[1] for p in group} for group in owners]
        for i,a in enumerate(articles):
            for b in articles[i+1:]:
                self.assertFalse(a&b)
        self.assertTrue(all(len(group)>0 for group in owners))

    def test_reviewer_range_selection(self):
        es=u.inventory([dict(path=f'data/{n:03d}.PMC{n}/a.pdf',sha=str(n)) for n in range(1,61)])
        keys=['fzm','ajk','mas','kf']
        seen=[]
        for key in keys:
            with patch.object(u,'REVIEWER_KEY',key):
                seen += [e['path'] for e in es if u.mine(e)]
        self.assertEqual(sorted(seen),sorted(e['path'] for e in es if int(e['path'].split('/')[1].split('.')[0]) <= 6164))
    def test_sharded_journal_file(self):
        with tempfile.TemporaryDirectory() as t, patch.object(u,'STATE',Path(t)):
            with patch.object(u,'REVIEWER_KEY','fzm'): u.record(self.e,'sent',message_id=9)
            self.assertTrue((Path(t)/'uploads.reviewer.fzm.jsonl').exists())
            self.assertFalse((Path(t)/'uploads.jsonl').exists())
            self.assertEqual(u.load_state()[self.e['path']]['message_id'],9)
    def test_journals_merge_by_time(self):
        # A path can move to another shard's journal if SHARDS changes; newest event must win
        # regardless of which file sorts last.
        with tempfile.TemporaryDirectory() as t, patch.object(u,'STATE',Path(t)):
            def write(name,status,when):
                with (Path(t)/name).open('a') as f:
                    f.write(json.dumps(dict(path=self.e['path'],sha='abc',status=status,time=when))+'\n')
            write('uploads.jsonl','failed','2026-11-08T09:00:00Z')
            write('uploads.shard1.jsonl','sent','2026-11-09T10:00:00Z')
            write('uploads.shard3.jsonl','failed','2026-11-09T09:00:00Z')
            self.assertEqual(u.load_state()[self.e['path']]['status'],'sent')
    def test_equal_timestamps_keep_file_order(self):
        with tempfile.TemporaryDirectory() as t, patch.object(u,'STATE',Path(t)):
            u.record(self.e,'inflight'); u.record(self.e,'uncertain')
            with patch.object(u.time,'strftime',return_value='2026-11-09T10:00:00Z'): u.record(self.e,'sent')
            self.assertEqual(u.load_state()[self.e['path']]['status'],'sent')
    def test_checkpoint_retries_rejected_push(self):
        calls=[]
        def fake_git(*args,**kw):
            calls.append(args[0])
            if args[0]=='push' and calls.count('push')<3: raise RuntimeError('Git operation failed: push')
        with tempfile.TemporaryDirectory() as t, patch.object(u,'STATE',Path(t)), patch.object(u,'git',side_effect=fake_git), \
             patch.object(u.subprocess,'run',return_value=types.SimpleNamespace(returncode=0)), patch.object(u.time,'sleep') as sleep:
            u.checkpoint()
        self.assertEqual(calls.count('push'),3); self.assertEqual(calls.count('pull'),3)
        self.assertEqual(sleep.call_count,2)
        for call in sleep.call_args_list: self.assertTrue(2<=call[0][0]<=10)
    def test_checkpoint_gives_up(self):
        def fake_git(*args,**kw):
            if args[0]=='push': raise RuntimeError('Git operation failed: push')
        with tempfile.TemporaryDirectory() as t, patch.object(u,'STATE',Path(t)), patch.object(u,'git',side_effect=fake_git) as git, \
             patch.object(u.subprocess,'run',return_value=types.SimpleNamespace(returncode=0)), patch.object(u.time,'sleep'):
            with self.assertRaises(RuntimeError): u.checkpoint()
        self.assertEqual([c[0][0] for c in git.call_args_list].count('push'),u.PUSH_ATTEMPTS)

    # --- pacing ---
    def test_pacing_counts_elapsed_time(self):
        with patch.object(u.time,'monotonic',side_effect=[10.0,12.1]),patch.object(u.time,'sleep') as sleep:
            self.assertEqual(u.pace(9.0,3.1),12.1)
        self.assertAlmostEqual(sleep.call_args[0][0],2.1)
    def test_pacing_no_wait_when_already_slow(self):
        with patch.object(u.time,'monotonic',side_effect=[20.0,20.0]),patch.object(u.time,'sleep') as sleep:
            u.pace(10.0,3.1)
        sleep.assert_not_called()
    def test_first_send_does_not_wait(self):
        with patch.object(u.time,'sleep') as sleep: u.pace(float('-inf'),3.1)
        sleep.assert_not_called()
