import hashlib
import http.server
from pathlib import Path
import tempfile
import threading
import unittest
import core

DATA=bytes(range(256))*8192
TAG='"fixture-v1"'
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        if self.path=='/denied':
            self.send_error(429);return
        tag='"different"' if self.path=='/changed' else TAG
        if self.headers.get('If-Match') and self.headers['If-Match']!=tag:
            self.send_error(412);return
        req=self.headers.get('Range')
        if req and self.path!='/plain':
            a,b=map(int,req[6:].split('-'))
            body=DATA[a:b+1]
            self.send_response(206)
            self.send_header('Content-Range','bytes %d-%d/%d'%(a,b,len(DATA)))
        else:
            body=DATA
            self.send_response(200)
        self.send_header('ETag',tag)
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        try:self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError):pass

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=cls.server.serve_forever,daemon=True).start()
        cls.url='http://127.0.0.1:%d'%cls.server.server_port
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close()
    def test_split(self):
        self.assertEqual(core.split_ranges(10,3,5),[(5,8),(9,11),(12,14)])
    def test_probe(self):
        info=core.probe(self.url+'/range')
        self.assertEqual(info['size'],len(DATA));self.assertTrue(info['range'])
        self.assertFalse(core.probe(self.url+'/plain')['range'])
    def test_parallel_hash(self):
        info=core.probe(self.url+'/range')
        with tempfile.TemporaryDirectory() as tmp:
            actual=core.native_sample(self.url+'/range',info,0,len(DATA),4,threading.Event(),Path(tmp))
        self.assertEqual(actual,hashlib.sha256(DATA).hexdigest())
    def test_rate_limit(self):
        with self.assertRaises(core.Stop):core.probe(self.url+'/denied')
    def test_changed(self):
        info=core.probe(self.url+'/range')
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(core.BenchmarkError):
                core.native_sample(self.url+'/changed',info,0,1024,2,threading.Event(),Path(tmp))
    def test_download(self):
        info=core.probe(self.url+'/range')
        with tempfile.TemporaryDirectory() as tmp:
            dst=Path(tmp)/'file.bin'
            got=core.download(self.url+'/range',info,{'engine':'native','connections':4},dst,threading.Event(),expected_sha=hashlib.sha256(DATA).hexdigest())
            self.assertEqual(dst.read_bytes(),DATA);self.assertTrue(got['trusted_hash_verified'])
            with self.assertRaises(core.BenchmarkError):core.download(self.url+'/range',info,None,dst,threading.Event())
    def test_recommend(self):
        rows=[dict(name=str(n),engine='native',connections=n,protocol='',status='OK',mib_s=v) for n,v in [(1,1),(4,9.7),(8,10)] for _ in range(2)]
        self.assertEqual(core.recommend(rows,2,3)[1]['connections'],4)
        self.assertIsNone(core.recommend(rows[:-1],2,3)[1])
    def test_no_range(self):
        info=core.probe(self.url+'/plain')
        self.assertIsNone(core.benchmark(self.url+'/plain',info,1,2,4,threading.Event())['recommendation'])
    def test_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=core.export_report(Path(tmp)/'report',{'host':'<test>','etag':'secret'},dict(rows=[],summary=[],recommendation=None,reason='test'))
            self.assertNotIn('secret',(r/'report.json').read_text(encoding='utf-8'))
            self.assertIn('&lt;test&gt;',(r/'report.html').read_text(encoding='utf-8'))
    def test_validation(self):
        for bad in ('file:///x','https://u:p@example.com/a','https://example.com/a b'):
            with self.assertRaises(core.BenchmarkError):core.check_url(bad)

if __name__=='__main__':unittest.main()
