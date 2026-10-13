import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch
from review import sync_service as service
from review.ledger import Ledger
from review.verified_sync import read_marker, write_marker, revision
from review.write_lock import Busy, write_lock
from review.sync_client import trigger, SyncUnconfirmed, verified_in_summary

class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=Path(self.temp.name)/'ledger.sqlite';l=Ledger(self.db);l.db.close()
        self.path=patch.object(service,'DATABASE',self.db);self.path.start();self.addCleanup(self.path.stop)
        self.env=patch.dict(os.environ,{'READER_TOKEN':'synthetic-read-token'});self.env.start();self.addCleanup(self.env.stop)
        self.original=service.command;self.calls=[];self.fail=None
        self.runner=patch.object(service,'command',side_effect=self.fake_command);self.runner.start();self.addCleanup(self.runner.stop)
    def fake_command(self,args,fd,capture=False):
        self.calls.append(args[0])
        if args[0]=='sync':
            l=Ledger(self.db)
            end=int(args[-1])
            try:
                l.ingest('trades',{'data':[],'pagination':{'next_offset':None}}, {'start':service.START,'stop':end,'offset':0})
                if self.fail=='sync':raise RuntimeError('SYNTHETIC_SECRET')
                for endpoint in ('account-book','order-history'):
                    l.ingest(endpoint,{'data':[],'pagination':{'next_offset':None}}, {'start':service.START,'stop':end,'offset':0})
            finally:l.db.close()
            return
        if args[0]==self.fail:raise RuntimeError('SYNTHETIC_SECRET')
        return self.original(args,fd,capture)
    def test_verified_duplicate_backup_and_inherited_cli_lock(self):
        key=str(uuid.uuid4());result=service.execute(key)
        self.assertEqual(result['request_id'],key);self.assertEqual(self.calls,['backup','sync','check'])
        service.execute(key);self.assertEqual(len(self.calls),3)
        backup=self.db.parent/'backups'/('before-'+key+'.sqlite')
        self.assertTrue(backup.exists());self.assertTrue(backup.with_suffix('.sha256').exists())
        db=sqlite3.connect(self.db);self.assertTrue(read_marker(self.db,db)['matches_current_ledger']);db.close()
    def test_marker_detects_later_fact_changes(self):
        key=str(uuid.uuid4());service.execute(key)
        db=sqlite3.connect(self.db);db.execute("INSERT INTO gaps(evidence_id,reason) VALUES(1,'synthetic later gap')");db.commit()
        marker=read_marker(self.db,db);db.close()
        self.assertFalse(marker['matches_current_ledger'])
        with self.assertRaises(service.SyncFailed):service.execute(key)

    def test_partial_failure_never_reuses_old_success(self):
        old=service.execute(str(uuid.uuid4()));self.fail='sync';new=str(uuid.uuid4())
        with self.assertRaises(RuntimeError):service.execute(new)
        db=sqlite3.connect(self.db);marker=read_marker(self.db,db);db.close()
        self.assertEqual(marker['request_id'],old['request_id']);self.assertFalse(marker['matches_current_ledger'])
        self.assertFalse(verified_in_summary(new,{'data':{'last_verified_sync':marker,'as_of':old['cutoff']}}))
    def test_check_failure_no_success_marker(self):
        self.fail='check'
        with self.assertRaises(RuntimeError):service.execute(str(uuid.uuid4()))
        db=sqlite3.connect(self.db);self.assertIsNone(read_marker(self.db,db));db.close()
    def test_parallel_cli_cannot_enter_writer_lock(self):
        with write_lock(self.db):
            p=subprocess.run([sys.executable,'-m','review','--db',str(self.db),'check'],capture_output=True,text=True)
            self.assertNotEqual(p.returncode,0)
    def test_thread_contention_no_queue(self):
        outcome=[]
        def contender():
            try:service.execute(str(uuid.uuid4()))
            except Busy:outcome.append('busy')
        with write_lock(self.db):
            t=threading.Thread(target=contender);t.start();t.join(3)
        self.assertEqual(outcome,['busy']);self.assertEqual(self.calls,[])
    def test_failed_backup_does_not_sync(self):
        self.fail='backup'
        with self.assertRaises(RuntimeError):service.execute(str(uuid.uuid4()))
        self.assertEqual(self.calls,['backup'])

class HttpTests(unittest.TestCase):
    def setUp(self):
        self.server=service.BoundedServer(('127.0.0.1',0),service.handler('sync-token'))
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
    def post(self,body,auth=['Bearer sync-token'],extra=[],method='POST',path='/review-sync'):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port)
        conn.putrequest(method,path)
        for a in auth:conn.putheader('Authorization',a)
        conn.putheader('Content-Type','application/json');conn.putheader('Content-Length',str(len(body)))
        for k,v in extra:conn.putheader(k,v)
        conn.endheaders(body);r=conn.getresponse();out=r.read();conn.close();return r.status,out
    def test_auth_methods_and_bad_inputs_do_not_execute(self):
        body=json.dumps({'request_id':str(uuid.uuid4())}).encode()
        with patch.object(service,'execute') as execute:
            for auth in ([],['Bearer reader-token'],['Bearer sync-token']*2):self.assertEqual(self.post(body,auth)[0],401)
            for method in ('GET','PUT','DELETE','PATCH','OPTIONS','HEAD'):self.assertEqual(self.post(body,method=method)[0],405)
            for extra in ([('Origin','https://evil')],[('Content-Length','1')],[('Transfer-Encoding','chunked')],[('Content-Type','text/plain')]):self.assertIn(self.post(body,extra=extra)[0],(400,403))
            for payload in (b'{}',b'{"request_id":"x"}',json.dumps({'request_id':str(uuid.uuid4()),'path':'/tmp'}).encode(),b'{"request_id":"x","request_id":"x"}'):
                self.assertEqual(self.post(payload)[0],400)
            self.assertFalse(execute.called)
    def test_busy_failure_and_success_redacted(self):
        key=str(uuid.uuid4());body=json.dumps({'request_id':key}).encode()
        with patch.object(service,'execute',side_effect=Busy()):self.assertEqual(self.post(body)[0],409)
        with patch.object(service,'execute',side_effect=RuntimeError('SYNTHETIC_SECRET')):
            status,out=self.post(body);self.assertEqual(status,500);self.assertNotIn(b'SYNTHETIC_SECRET',out)
        with patch.object(service,'execute',return_value={'request_id':key}):self.assertEqual(self.post(body)[0],200)
    def test_disconnect_does_not_cancel_action(self):
        entered,release,completed=threading.Event(),threading.Event(),threading.Event()
        def action(key):entered.set();release.wait(3);completed.set();return {'request_id':key}
        body=json.dumps({'request_id':str(uuid.uuid4())}).encode()
        with patch.object(service,'execute',side_effect=action):
            with socket.create_connection(('127.0.0.1',self.server.server_port)) as conn:
                request=(f'POST /review-sync HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\nAuthorization: Bearer sync-token\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n').encode()+body
                conn.sendall(request);self.assertTrue(entered.wait(2))
            release.set();self.assertTrue(completed.wait(2))

class ClientTests(unittest.TestCase):
    def test_timeout_single_post_no_auto_retry(self):
        with patch.dict(os.environ,{'REVIEW_SYNC_TOKEN':'synthetic'}),patch('urllib.request.build_opener') as factory:
            factory.return_value.open.side_effect=TimeoutError('SYNTHETIC_SECRET')
            with self.assertRaises(SyncUnconfirmed) as e:trigger(str(uuid.uuid4()))
            self.assertNotIn('SYNTHETIC_SECRET',str(e.exception));self.assertEqual(factory.return_value.open.call_count,1)
    def test_old_or_changed_marker_never_confirms(self):
        key=str(uuid.uuid4());old=str(uuid.uuid4())
        for marker in (None,{'request_id':old,'matches_current_ledger':True,'cutoff':1},{'request_id':key,'matches_current_ledger':False,'cutoff':1}):
            self.assertFalse(verified_in_summary(key,{'data':{'as_of':2,'last_verified_sync':marker}}))
