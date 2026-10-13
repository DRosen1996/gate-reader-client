"""Fixed authenticated sync action. No scheduler, shell, queue or write UI."""
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .ledger import private_path, verify_database
from .verified_sync import read_marker, request_id, revision, write_marker
from .write_lock import Busy, write_lock

DATABASE = Path('/review-data/ledger.sqlite')
START = 1788192000
SOURCE = Path(__file__).resolve().parents[1]
class SyncFailed(Exception):
    pass


def command(args, fd, capture=False):
    reader = os.environ.get('READER_TOKEN', '')
    if not reader:
        raise SyncFailed()
    env = {k:v for k,v in os.environ.items() if k.lower() in {'https_proxy','http_proxy','no_proxy','ssl_cert_file','ssl_cert_dir'}}
    env.update(READER_TOKEN=reader, REVIEW_LOCK_FD=str(fd), PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable,'-m','review','--db',str(DATABASE),*args],
        cwd=SOURCE, env=env, pass_fds=(fd,), stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, text=True)
    if result.returncode:
        raise SyncFailed()
    return result.stdout if capture else None


def execute(identifier):
    request_id(identifier)
    private_path(DATABASE)
    if not DATABASE.is_file():
        raise SyncFailed()
    with write_lock(DATABASE) as fd:
        db=sqlite3.connect(DATABASE.as_uri()+'?mode=ro',uri=True)
        try:
            old=read_marker(DATABASE,db)
        finally: db.close()
        if old and old['request_id']==identifier:
            if not old['matches_current_ledger']:
                raise SyncFailed()
            return old
        end=int(time.time())
        backup=DATABASE.parent/'backups'/('before-'+identifier+'.sqlite')
        # Existing backup is never overwritten; interrupted requests are not
        # retried implicitly. A new user intent uses a new identifier.
        digest=command(['backup',str(backup)],fd,True).strip()
        if not re.fullmatch('[a-f0-9]{64}',digest): raise SyncFailed()
        receipt=os.open(backup.with_suffix('.sha256'),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(receipt,'w') as out:
            out.write(digest+'\n');out.flush();os.fsync(out.fileno())
        command(['sync','--start',str(START),'--end',str(end)],fd)
        command(['check'],fd)
        db=sqlite3.connect(DATABASE.as_uri()+'?mode=ro',uri=True)
        db.row_factory=sqlite3.Row
        try:
            verify_database(db)
            from .ledger import Ledger
            projection=object.__new__(Ledger);projection.db=db
            cuts=projection.view()['history_cutoffs']
            if any(cuts.get(k)!=end for k in ('trades','account-book','order-history')):
                raise SyncFailed()
            success={'request_id':identifier,'cutoff':end,'checked_at':int(time.time()),'revision':revision(db)}
        finally:db.close()
        write_marker(DATABASE,success)
        return dict(success,matches_current_ledger=True)


def handler(token):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup(); self.connection.settimeout(5)
        def log_message(self,*args): pass
        def send(self,status,data):
            self.close_connection=True
            body=json.dumps(data,separators=(',', ':')).encode()
            try:
                self.send_response(status);self.send_header('Content-Type','application/json')
                self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body)
            except OSError: pass  # disconnect never converts committed success to failure
        def do_POST(self):
            auth=self.headers.get_all('Authorization',[])
            if len(auth)!=1 or not hmac.compare_digest(auth[0].encode(),('Bearer '+token).encode()):
                return self.send(401,{'error':'Unauthorized'})
            hosts=self.headers.get_all('Host',[])
            if len(hosts)!=1 or hosts[0] not in {'gate-reader.gatelens.site','127.0.0.1:'+str(self.server.server_port),'localhost:'+str(self.server.server_port)} or self.headers.get_all('Origin'):
                return self.send(403,{'error':'Forbidden'})
            if self.path!='/review-sync':return self.send(404,{'error':'Unknown action'})
            lengths=self.headers.get_all('Content-Length',[])
            types=self.headers.get_all('Content-Type',[])
            if self.headers.get_all('Transfer-Encoding') or len(lengths)!=1 or not re.fullmatch('[0-9]{1,3}',lengths[0]) or not 0<int(lengths[0])<=128 or types!=['application/json']:
                return self.send(400,{'error':'Invalid request'})
            try:
                def pairs(items):
                    if len(dict(items))!=len(items):raise ValueError()
                    return dict(items)
                data=json.loads(self.rfile.read(int(lengths[0])),object_pairs_hook=pairs)
                if not isinstance(data,dict) or set(data)!={'request_id'}:raise ValueError()
                identifier=request_id(data['request_id'])
            except Exception:return self.send(400,{'error':'Invalid request'})
            try: result=execute(identifier)
            except Busy:return self.send(409,{'error':'Sync busy; no queued request'})
            except Exception:return self.send(500,{'error':'Sync unconfirmed; inspect read-only summary'})
            self.send(200,{'ok':True,'last_verified_sync':result})
        def reject(self):self.send(405,{'error':'Fixed POST only'})
        do_GET=do_PUT=do_DELETE=do_PATCH=do_OPTIONS=do_HEAD=reject
    return Handler


class BoundedServer(ThreadingHTTPServer):
    def __init__(self,*args,**kwargs):
        self.slots=threading.BoundedSemaphore(8)
        super().__init__(*args,**kwargs)
    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try: super().process_request(request,address)
        except BaseException:
            self.slots.release()
            raise
    def process_request_thread(self,request,address):
        try: super().process_request_thread(request,address)
        finally: self.slots.release()


def main():
    token=os.environ.get('REVIEW_SYNC_TOKEN',''); reader=os.environ.get('READER_TOKEN','')
    if not re.fullmatch('[A-Za-z0-9_-]{32,128}',token) or not reader or hmac.compare_digest(token,reader):
        raise SystemExit('Distinct protected sync/read credentials required')
    os.umask(0o077)
    BoundedServer(('127.0.0.1',8788),handler(token)).serve_forever()

if __name__=='__main__':main()
