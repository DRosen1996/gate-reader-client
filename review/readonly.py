"""Fixed-path, bounded read-only projection for the authenticated reader."""
import json
import re
import stat
import sqlite3
import threading
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
from .ledger import Ledger, SCHEMA_VERSION

DATABASE = Path('/review-data/ledger.sqlite')
MAX_RESPONSE = 512 * 1024
READ_SLOTS = threading.BoundedSemaphore(2)
ROW_LIMITS = {"observations": 5000, "evidence": 10000, "sightings": 20000,
              "notes": 2000, "ranges": 2000, "gaps": 2000, "cycle_aliases": 5000}

class ReviewError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message

class ReadOnlyLedger(Ledger):
    def __init__(self):
        # Deliberately do not call Ledger.__init__: no file creation or migration.
        info = DATABASE.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024 * 1024:
            raise ReviewError(503, 'Ledger unavailable')
        self.db = sqlite3.connect(DATABASE.as_uri() + '?mode=ro', uri=True, timeout=1)
        try:
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA query_only=ON')
            self.db.execute('PRAGMA trusted_schema=OFF')
            if self.db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
                raise ReviewError(503, 'Ledger unavailable')
            if self.db.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'delete':
                raise ReviewError(503, 'Ledger unavailable')
            self.db.set_authorizer(lambda action, a, b, c, d: sqlite3.SQLITE_OK
                if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                              sqlite3.SQLITE_TRANSACTION) and not (action == sqlite3.SQLITE_FUNCTION and b == 'load_extension')
                else sqlite3.SQLITE_DENY)
            budget = [0]
            def progress():
                budget[0] += 1
                return budget[0] > 5000
            self.db.set_progress_handler(progress, 1000)
        except BaseException:
            self.db.close()
            raise

    def view(self):
        self.db.execute('BEGIN')
        try:
            # The accounting projection also does Python work; SQL progress alone
            # cannot bound it. Limit source cardinality before loading JSON.
            for table, maximum in ROW_LIMITS.items():
                count = self.db.execute('SELECT COUNT(*) FROM (SELECT 1 FROM ' + table + ' LIMIT ?)', (maximum + 1,)).fetchone()[0]
                if count > maximum:
                    raise ReviewError(503, 'Ledger unavailable')
            return super().view()
        finally:
            self.db.rollback()


def parameters(target):
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path not in ('/review-summary', '/review-trade'):
        raise ReviewError(400, 'Invalid review request')
    try:
        pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=3)
    except ValueError:
        raise ReviewError(400, 'Invalid review request') from None
    params = dict(pairs)
    allowed = {'limit', 'offset'} | ({'id'} if parsed.path == '/review-trade' else set())
    if len(pairs) != len(params) or params.keys() - allowed:
        raise ReviewError(400, 'Invalid review request')
    for key, default, maximum in (('limit', '20', 100), ('offset', '0', 10000)):
        value = params.get(key, default)
        if not re.fullmatch(r'[0-9]{1,5}', value) or not (int(key == 'limit') <= int(value) <= maximum):
            raise ReviewError(400, 'Invalid review request')
        params[key] = int(value)
    if parsed.path == '/review-trade' and not re.fullmatch(r'cycle:[a-f0-9]{64}', params.get('id', '')):
        raise ReviewError(400, 'Invalid review request')
    return parsed.path, params


def query_review(target):
    route, params = parameters(target)
    if not READ_SLOTS.acquire(blocking=False):
        raise ReviewError(503, 'Ledger unavailable')
    try:
        return _query_review(route, params)
    finally:
        READ_SLOTS.release()


def _query_review(route, params):
    ledger = None
    try:
        ledger = ReadOnlyLedger()
        view = ledger.view()
        start, limit = params['offset'], params['limit']
        def page(items):
            return {'items': items[start:start+limit], 'total': len(items),
                    'next_offset': start+limit if start+limit < len(items) else None}
        data = {key: view[key] for key in ('as_of', 'observed_at', 'history_cutoffs', 'coverage',
            'metrics_status', 'metrics_reasons', 'completeness', 'currency')}
        data['source'] = 'server SQLite ledger'
        if route == '/review-summary':
            for key in ('cash_categories', 'realised_pnl', 'period_trading_cash_net', 'deposits', 'withdrawals', 'unrealised', 'balance_bridge'):
                data[key] = view[key]
            data['cycles'] = page([{k: c[k] for k in ('id', 'contract', 'opened', 'closed', 'size', 'entry', 'plan', 'realised_components')} for c in view['cycles']])
            for key in ('gaps', 'unallocated', 'ranges', 'orphan_notes'):
                data[key] = page(view[key])
        else:
            cycle = next((c for c in view['cycles'] if c['id'] == params['id']), None)
            if cycle is None:
                raise ReviewError(404, 'Trade not found')
            data['trade'] = {k: page(v) if isinstance(v, list) else v for k, v in cycle.items()}
        body = json.dumps({'ok': True, 'data': data}, ensure_ascii=False, allow_nan=False).encode()
        if len(body) > MAX_RESPONSE:
            raise ReviewError(413, 'Review response too large; use smaller limit')
        return body
    except ReviewError:
        raise
    except Exception:
        raise ReviewError(503, 'Ledger unavailable') from None
    finally:
        if ledger is not None:
            ledger.db.close()
