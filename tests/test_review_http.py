"""HTTP protections with a subprocess and exclusively synthetic local data."""
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / 'demo.db')
        subprocess.run([sys.executable, '-m', 'review', '--db', cls.db, 'demo'], check=True, stdout=subprocess.DEVNULL)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            cls.port = sock.getsockname()[1]
        cls.process = subprocess.Popen([sys.executable, '-m', 'review', '--db', cls.db, 'serve', '--port', str(cls.port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                with socket.create_connection(('127.0.0.1', cls.port), timeout=.1):
                    return
            except OSError:
                time.sleep(.05)
        raise RuntimeError('Synthetic server did not start')

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=5)
        cls.tmp.cleanup()

    def request(self, method, path, headers=None, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        result = connection.getresponse()
        status, response_headers, content = result.status, dict(result.getheaders()), result.read()
        connection.close()
        return status, response_headers, content

    def view(self):
        return json.loads(self.request('GET', '/api/view')[2])

    def test_note_during_sync_lock_returns_busy(self):
        from review.write_lock import write_lock
        cycle=self.view()['cycles'][0]['id']
        body=json.dumps({'cycle':cycle,'kind':'review','body':'synthetic blocked note'})
        headers={'Origin':f'http://127.0.0.1:{self.port}','Content-Type':'application/json'}
        with write_lock(self.db):
            self.assertEqual(self.request('POST','/api/notes',headers,body)[0],409)

    def test_host_origin_rejections_do_not_write(self):
        cycle = self.view()['cycles'][0]['id']
        before = len(self.view()['cycles'][0]['notes'])
        body = json.dumps({'cycle': cycle, 'kind': 'plan', 'body': 'synthetic'})
        origin = f'http://127.0.0.1:{self.port}'
        cases = [
            {'Host': f'evil.example:{self.port}', 'Origin': origin},
            {}, {'Origin': 'null'}, {'Origin': 'https://evil.example'},
            {'Origin': f'http://localhost:{self.port}'},
            {'Origin': origin, 'Sec-Fetch-Site': 'cross-site'},
        ]
        for headers in cases:
            status = self.request('POST', '/api/notes', dict(headers, **{'Content-Type': 'application/json'}), body)[0]
            self.assertEqual(status, 403)
        self.assertEqual(len(self.view()['cycles'][0]['notes']), before)

    def test_valid_note_is_always_retrospective(self):
        cycle = self.view()['cycles'][0]['id']
        body = json.dumps({'cycle': cycle, 'kind': 'plan', 'body': 'synthetic accepted'})
        status = self.request('POST', '/api/notes', {'Origin': f'http://127.0.0.1:{self.port}', 'Content-Type': 'application/json'}, body)[0]
        self.assertEqual(status, 201)
        note = self.view()['cycles'][0]['notes'][-1]
        self.assertEqual(note['retrospective'], 1)
        self.assertEqual(note['body'], 'synthetic accepted')

    def test_content_type_size_and_unknown_cycle_rejected(self):
        origin = f'http://127.0.0.1:{self.port}'
        for headers, body in [
            ({'Content-Type': 'text/plain'}, '{}'),
            ({'Content-Type': 'application/json'}, 'x' * 20001),
            ({'Content-Type': 'application/json'}, json.dumps({'cycle': 'unknown', 'kind': 'plan', 'body': 'synthetic'})),
            ({'Content-Type': 'application/json'}, '[]'),
        ]:
            self.assertEqual(self.request('POST', '/api/notes', dict(headers, Origin=origin), body)[0], 400)

    def test_get_host_and_security_headers(self):
        self.assertEqual(self.request('GET', '/api/view', {'Host': 'evil.example'})[0], 403)
        status, headers, _ = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertNotIn('Access-Control-Allow-Origin', headers)

    def test_duplicate_headers_and_transfer_encoding_do_not_write(self):
        before = len(self.view()['cycles'][0]['notes'])
        cycle = self.view()['cycles'][0]['id']
        body = json.dumps({'cycle': cycle, 'kind': 'plan', 'body': 'must reject'}).encode()
        host = f'127.0.0.1:{self.port}'
        origin = 'http://' + host
        cases = [
            (f'Host: {host}\r\n', 403),
            (f'Origin: {origin}\r\n', 403),
            (f'Content-Length: {len(body)}\r\n', 400),
            ('Transfer-Encoding: chunked\r\n', 400),
        ]
        for extra, expected in cases:
            request = (f'POST /api/notes HTTP/1.0\r\nHost: {host}\r\nOrigin: {origin}\r\n'
                       f'Content-Type: application/json\r\nContent-Length: {len(body)}\r\n{extra}\r\n').encode() + body
            with socket.create_connection(('127.0.0.1', self.port), timeout=5) as connection:
                connection.sendall(request)
                status = connection.recv(4096).split(b'\r\n', 1)[0]
            self.assertIn(str(expected).encode(), status)
        self.assertEqual(len(self.view()['cycles'][0]['notes']), before)
