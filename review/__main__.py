"""Local-only CLI and HTTP service. No reader or exchange writes are exposed."""
import argparse
import json
import sqlite3
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .ledger import Ledger, restore, verify_database
from .write_lock import Busy


def handler_for(ledger, port):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            pass

        def send(self, status, body, content_type='application/json'):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            hosts = self.headers.get_all('Host', [])
            return (len(hosts) == 1 and hosts[0] in {f'127.0.0.1:{port}', f'localhost:{port}'}
                    and self.headers.get('Sec-Fetch-Site') != 'cross-site')

        def do_GET(self):
            if not self.authorized():
                return self.send(403, b'{}')
            route = urlsplit(self.path).path
            if route == '/api/view':
                try:
                    payload = json.dumps(ledger.view(), ensure_ascii=False).encode()
                except (ValueError, ArithmeticError, sqlite3.DatabaseError):
                    return self.send(500, b'{"error":"Ledger unavailable"}')
                return self.send(200, payload)
            filename = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}.get(route)
            if filename:
                content_type = {'index.html': 'text/html; charset=utf-8', 'app.js': 'text/javascript', 'style.css': 'text/css'}[filename]
                return self.send(200, (Path(__file__).parent / 'static' / filename).read_bytes(), content_type)
            self.send(404, b'{}')

        def do_POST(self):
            origins = self.headers.get_all('Origin', [])
            if not self.authorized() or origins != ['http://' + self.headers.get('Host', '')]:
                return self.send(403, b'{}')
            if self.path != '/api/notes':
                return self.send(404, b'{}')
            if (self.headers.get('Content-Type', '').partition(';')[0].strip().lower() != 'application/json'
                    or self.headers.get('Transfer-Encoding') is not None
                    or len(self.headers.get_all('Content-Length', [])) != 1):
                return self.send(400, b'{"error":"Invalid request"}')
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 20000:
                    raise ValueError()
                note = json.loads(self.rfile.read(size))
                cycle = next(c for c in ledger.view()['cycles'] if c['id'] == note['cycle'])
                ledger.note(cycle['id'], note['kind'], note['body'], int(Decimal(str(cycle['opened']))))
                self.send(201, b'{}')
            except Busy:
                self.send(409, b'{"error":"Ledger busy"}')
            except (ValueError, KeyError, StopIteration, TypeError, ArithmeticError, TimeoutError):
                self.send(400, b'{"error":"Invalid note"}')
            except sqlite3.DatabaseError:
                self.send(500, b'{"error":"Ledger unavailable"}')
    return Handler


def _main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    sync = sub.add_parser('sync')
    sync.add_argument('--start', type=int, required=True)
    sync.add_argument('--end', type=int, default=None)
    backup = sub.add_parser('backup')
    backup.add_argument('target')
    recover = sub.add_parser('restore')
    recover.add_argument('source')
    recover.add_argument('--sha256', required=True)
    sub.add_parser('check')
    sub.add_parser('demo')
    serve = sub.add_parser('serve')
    serve.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if args.command == 'restore':
        restore(args.source, args.db, args.sha256)
        print('Verified private restore complete')
        return
    if args.command == 'serve' and not 1 <= args.port <= 65535:
        parser.error('Invalid port')
    ledger = Ledger(args.db)
    try:
        if args.command == 'sync':
            from gate_reader import Client
            end = int(time.time()) if args.end is None else args.end
            client = Client()
            for endpoint in ('trades', 'account-book', 'order-history'):
                ledger.sync(client, endpoint, args.start, end)
            for endpoint in ('account', 'positions', 'orders', 'snapshot'):
                if endpoint == 'orders':
                    for page in client.pages(endpoint):
                        ledger.ingest(endpoint, page, {})
                else:
                    ledger.ingest(endpoint, client.query(endpoint), {})
            print('Read-only sync committed; inspect private local UI for completeness')
        elif args.command == 'backup':
            print(ledger.backup(args.target))
        elif args.command == 'check':
            verify_database(ledger.db)
            print(json.dumps({'integrity': 'ok', 'evidence_digests': 'ok', 'sqlite': sqlite3.sqlite_version,
                              'schema': ledger.db.execute('PRAGMA user_version').fetchone()[0],
                              'journal': ledger.db.execute('PRAGMA journal_mode').fetchone()[0],
                              'foreign_keys': 'ok'}))
        elif args.command == 'demo':
            ledger.ingest('trades', {'pagination': {'next_offset': None}, 'data': [
                {'id': 'demo1', 'order_id': 'order1', 'contract': 'DEMO_USDT', 'size': '2', 'price': '100', 'fee': '-0.1', 'fee_currency': 'USDT', 'create_time': 1700000000, 'text': '<img src=x onerror=window.__sourceXss=1>'},
                {'id': 'demo2', 'order_id': 'order2', 'contract': 'DEMO_USDT', 'size': '-3', 'price': '110', 'fee': '-0.15', 'fee_currency': 'USDT', 'create_time': 1700000060},
                {'id': 'demo3', 'order_id': 'order3', 'contract': 'DEMO_USDT', 'size': '1', 'price': '105', 'fee': '-0.05', 'fee_currency': 'USDT', 'create_time': 1700000120},
            ]}, {'synthetic': True, 'start': 1699999999, 'stop': 1700000120, 'offset': 0})
            ledger.ingest('account-book', {'pagination': {'next_offset': None}, 'data': [
                {'id': '0', 'contract': '', 'type': 'dnw', 'time': 1699999999, 'change': '1000', 'balance': '1000'},
                {'id': '1', 'trade_id': 'demo1', 'contract': 'DEMO_USDT', 'type': 'fee', 'time': 1700000000, 'change': '-0.1', 'balance': '999.9'},
                {'id': '2', 'trade_id': 'demo2', 'contract': 'DEMO_USDT', 'type': 'fee', 'time': 1700000060, 'change': '-0.15', 'balance': '999.75'},
                {'id': '3', 'trade_id': 'demo2', 'contract': 'DEMO_USDT', 'type': 'pnl', 'time': 1700000060, 'change': '20', 'balance': '1019.75'},
                {'id': '4', 'trade_id': 'demo3', 'contract': 'DEMO_USDT', 'type': 'fee', 'time': 1700000120, 'change': '-0.05', 'balance': '1019.7'},
                {'id': '5', 'trade_id': 'demo3', 'contract': 'DEMO_USDT', 'type': 'pnl', 'time': 1700000120, 'change': '5', 'balance': '1024.7'},
            ]}, {'synthetic': True, 'start': 1699999999, 'stop': 1700000120, 'offset': 0})
            ledger.ingest('order-history', {'pagination': {'next_offset': None}, 'data': [
                {'id': 'order1', 'contract': 'DEMO_USDT', 'status': 'finished', 'size': '2'},
                {'id': 'order2', 'contract': 'DEMO_USDT', 'status': 'finished', 'size': '-3'},
                {'id': 'order3', 'contract': 'DEMO_USDT', 'status': 'finished', 'size': '1'},
            ]}, {'synthetic': True, 'start': 1699999999, 'stop': 1700000120, 'offset': 0})
            ledger.ingest('account', {'data': {'currency': 'USDT', 'total': '1024.7', 'unrealised_pnl': '0', 'in_dual_mode': False}}, {'synthetic': True})
        elif args.command == 'serve':
            with HTTPServer(('127.0.0.1', args.port), handler_for(ledger, args.port)) as server:
                print(f'Private local review: http://127.0.0.1:{args.port}', flush=True)
                server.serve_forever()
    finally:
        ledger.db.close()


def main():
    # Parse only the fixed CLI database option for locking; _main retains full
    # argparse validation. Serving holds the lock only during initialization.
    import sys
    from .write_lock import write_lock
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--db', required=True)
    args, rest = parser.parse_known_args()
    if rest and rest[0] == 'serve':
        return _main()
    with write_lock(args.db):
        return _main()


if __name__ == '__main__':
    main()
