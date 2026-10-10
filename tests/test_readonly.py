import hashlib
import http.client
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from http.server import ThreadingHTTPServer
from review import readonly
from reader_bridge.app import Handler

class ReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'ledger.sqlite'
        subprocess.run([sys.executable, '-m', 'review', '--db', str(self.path), 'demo'], check=True, stdout=subprocess.DEVNULL)
        self.patch = patch.object(readonly, 'DATABASE', self.path)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def test_reads_notes_without_mutation_or_ddl(self):
        before = (self.path.stat().st_mtime_ns, hashlib.sha256(self.path.read_bytes()).hexdigest())
        result = json.loads(readonly.query_review('/review-summary?limit=1'))['data']
        cid = result['cycles']['items'][0]['id']
        db = sqlite3.connect(self.path)
        db.execute("INSERT INTO notes(cycle,kind,body,recorded,effective,retrospective) VALUES(?,?,?,?,?,1)", (cid, 'review', 'synthetic note', 1, 1)); db.commit(); db.close()
        before = (self.path.stat().st_mtime_ns, hashlib.sha256(self.path.read_bytes()).hexdigest())
        result = json.loads(readonly.query_review('/review-trade?id='+cid))['data']['trade']
        self.assertEqual(result['notes']['items'][0]['body'], 'synthetic note')
        ro = readonly.ReadOnlyLedger()
        try:
            for sql in ('CREATE TABLE bad(x)', 'DELETE FROM notes', "ATTACH ':memory:' AS bad"):
                with self.assertRaises(sqlite3.DatabaseError): ro.db.execute(sql)
        finally: ro.db.close()
        self.assertEqual(before, (self.path.stat().st_mtime_ns, hashlib.sha256(self.path.read_bytes()).hexdigest()))

    def test_validation_and_bounds(self):
        for target in ('/review-summary?sql=SELECT', '/review-summary?path=x', '/review-summary?offset=-1', '/review-summary?limit=101', '/review-summary?limit=1&limit=2', '/review-trade', '/review-trade?id=../x'):
            with self.assertRaises(readonly.ReviewError) as e: readonly.query_review(target)
            self.assertEqual(e.exception.status, 400)
        with self.assertRaises(readonly.ReviewError) as e: readonly.query_review('/review-trade?id=cycle:'+'0'*64)
        self.assertEqual(e.exception.status, 404)
        with patch.object(readonly, 'MAX_RESPONSE', 1), self.assertRaises(readonly.ReviewError) as e: readonly.query_review('/review-summary')
        self.assertEqual(e.exception.status, 413)

    def test_safe_failures(self):
        db=sqlite3.connect(self.path); db.execute('PRAGMA user_version=99'); db.close()
        with self.assertRaises(readonly.ReviewError) as e: readonly.query_review('/review-summary')
        self.assertEqual(str(e.exception), "(503, 'Ledger unavailable')")
        self.path.unlink()
        with self.assertRaises(readonly.ReviewError): readonly.query_review('/review-summary')
        self.assertFalse(self.path.exists())
        self.path.write_bytes(b'corrupt synthetic data')
        with self.assertRaises(readonly.ReviewError): readonly.query_review('/review-summary')

    def test_resource_and_mode_limits(self):
        with patch.object(readonly, 'ROW_LIMITS', {'observations': 0}), self.assertRaises(readonly.ReviewError):
            readonly.query_review('/review-summary')
        readonly.READ_SLOTS.acquire(); readonly.READ_SLOTS.acquire()
        try:
            with self.assertRaises(readonly.ReviewError): readonly.query_review('/review-summary')
        finally: readonly.READ_SLOTS.release(); readonly.READ_SLOTS.release()
        ro=readonly.ReadOnlyLedger()
        try:
            ro.db.set_progress_handler(lambda: 1, 1)
            with self.assertRaises(sqlite3.DatabaseError): ro.view()
        finally: ro.db.close()
        db=sqlite3.connect(self.path); db.execute('PRAGMA journal_mode=WAL');db.close()
        with self.assertRaises(readonly.ReviewError): readonly.query_review('/review-summary')

    def test_symlink_and_file_limit(self):
        link=Path(self.temp.name)/'link.sqlite';link.symlink_to(self.path)
        with patch.object(readonly, 'DATABASE', link), self.assertRaises(readonly.ReviewError):
            readonly.query_review('/review-summary')
        with self.path.open('r+b') as f: f.truncate(64*1024*1024+1)
        with self.assertRaises(readonly.ReviewError): readonly.query_review('/review-summary')

    def test_client_review_validation(self):
        from gate_reader import Client, ReaderError
        client = Client()
        for params in ({'id': '../x'}, {'id': 'cycle:'+'0'*64, 'path': 'x'}, {'id': 'cycle:'+'0'*64, 'offset': 10001}):
            with self.assertRaises(ReaderError): client.query('review-trade', **params)
        with self.assertRaises(ReaderError): next(client.pages('review-summary'))
        with patch.object(client._opener, 'open') as transport:
            transport.return_value.__enter__.return_value.geturl.return_value = 'https://gate-reader.gatelens.site/review-summary?limit=1'
            transport.return_value.__enter__.return_value.status = 200
            transport.return_value.__enter__.return_value.read.return_value = b'{"ok":true,"data":{}}'
            with patch.dict('os.environ', {'READER_TOKEN': 'synthetic'}):
                self.assertTrue(client.query('review-summary', limit=1)['ok'])

    def test_locked_database(self):
        db=sqlite3.connect(self.path); db.execute('BEGIN EXCLUSIVE')
        try:
            with self.assertRaises(readonly.ReviewError) as e: readonly.query_review('/review-summary')
            self.assertEqual(e.exception.status, 503)
        finally: db.rollback(); db.close()

    def test_http_auth_and_write_rejection_original_route(self):
        server=ThreadingHTTPServer(('127.0.0.1',0), Handler); server.token='synthetic-token'; server.reader=Mock()
        server.reader.read.return_value={'ok':True,'data':[]}
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        def call(method, path, auth):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
            conn.putrequest(method,path)
            for value in auth: conn.putheader('Authorization',value)
            conn.endheaders(); response=conn.getresponse(); data=response.read(); headers=dict(response.getheaders());conn.close()
            return response.status, headers, data
        for path in ('/review-summary','/review-trade?id=cycle:'+'0'*64, '/account'):
            for auth in ([], ['Bearer wrong'], ['Bearer synthetic-token']*2):
                self.assertEqual(call('GET',path,auth)[0],401)
        self.assertEqual(call('GET','/account',['Bearer synthetic-token'])[0],200)
        status,headers,data=call('GET','/review-summary',['Bearer synthetic-token'])
        self.assertEqual(status,200);self.assertEqual(headers['Cache-Control'],'no-store')
        for method in ('POST','PUT','PATCH','DELETE','OPTIONS','HEAD'):
            self.assertEqual(call(method,'/review-summary',['Bearer synthetic-token'])[0],405)
        self.assertEqual(call('GET','/api/notes',['Bearer synthetic-token'])[0],404)
        with socket.create_connection(('127.0.0.1',server.server_port)) as raw:
            raw.sendall(b'GET http://[ HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer synthetic-token\r\nConnection: close\r\n\r\n')
            self.assertIn(b' 400 ', raw.recv(4096).split(b'\r\n')[0])
        with patch.object(readonly, 'query_review', side_effect=RuntimeError('SYNTHETIC_SECRET')):
            status,headers,body=call('GET','/review-summary',['Bearer synthetic-token'])
            self.assertEqual(status,503);self.assertNotIn(b'SYNTHETIC_SECRET',body)


if __name__ == '__main__': unittest.main()
