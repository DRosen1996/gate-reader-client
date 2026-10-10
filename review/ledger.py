"""Private append-only evidence and reproducible, provisional cash accounting."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time
from decimal import Decimal, localcontext

SCHEMA_VERSION = 2
HISTORICAL_ENDPOINTS = ('trades', 'account-book', 'order-history')


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def decimal(value):
    if isinstance(value, (float, bool)):
        raise ValueError('Money must be decimal text or integer')
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('Non-finite money')
    return result


def source_order(value):
    text = str(value)
    return (0, int(text)) if text.isdigit() else (1, text)


def trade_identifier(record):
    value = record.get('trade_id')
    return str(record.get('id', '')) if value is None else str(value)


def fill_key(record):
    return str(record['contract']) + ':' + trade_identifier(record)


def cycle_key(record):
    identity = encode([record['contract'], trade_identifier(record)])
    return 'cycle:' + hashlib.sha256(identity.encode()).hexdigest()


def private_path(value):
    """Refuse unsafe existing paths; never change their permissions."""
    path = Path(value).resolve()
    repo = Path(__file__).resolve().parents[1]
    if path == repo or repo in path.parents:
        raise ValueError('Private data must be outside repository')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for entry in (path.parent, path) if path.exists() else (path.parent,):
        info = entry.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError('Private data path requires owner-only permissions')
        if entry == path and not stat.S_ISREG(info.st_mode):
            raise ValueError('Private data must be a regular file')
    return path


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def verify_database(connection):
    if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise ValueError('Database integrity failed')
    if connection.execute('PRAGMA foreign_key_check').fetchall():
        raise ValueError('Database references failed')
    if connection.execute('PRAGMA user_version').fetchone()[0] not in (1, SCHEMA_VERSION):
        raise ValueError('Unsupported database schema')
    for table in ('evidence', 'observations'):
        for body, digest in connection.execute(f'SELECT body,digest FROM {table}'):
            if hashlib.sha256(body.encode()).hexdigest() != digest:
                raise ValueError('Evidence digest mismatch')
    missing = connection.execute('''SELECT 1 FROM sightings s LEFT JOIN observations o
        USING(endpoint,source_id,digest) WHERE o.source_id IS NULL LIMIT 1''').fetchone()
    if missing:
        raise ValueError('Observation reference failed')


def coverage(ranges):
    """Only a continuous union of completed windows advances the cutoff."""
    if not ranges:
        return {e: {'status': 'unknown', 'contiguous_to': None, 'missing': []}
                for e in HISTORICAL_ENDPOINTS}
    start = min(r['start'] for r in ranges)
    end = max(r['stop'] for r in ranges)
    result = {}
    for endpoint in HISTORICAL_ENDPOINTS:
        intervals = sorted((r['start'], r['stop']) for r in ranges
                           if r['endpoint'] == endpoint and r['state'] == 'complete')
        cursor = start
        contiguous = start
        missing = []
        interrupted = False
        for left, right in intervals:
            if left > cursor:
                missing.append([cursor, left])
                interrupted = True
            cursor = max(cursor, right)
            if not interrupted:
                contiguous = cursor
        if cursor < end:
            missing.append([cursor, end])
        has_start = any(left <= start <= right for left, right in intervals)
        result[endpoint] = {
            'status': 'read_complete' if has_start and not missing else 'partial',
            'requested_from': start, 'requested_to': end,
            'contiguous_to': contiguous if has_start else None,
            'missing': missing,
        }
    return result


class Ledger:
    def __init__(self, path):
        self.path = private_path(path)
        os.umask(0o077)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            private_path(self.path)
        else:
            os.close(fd)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1, SCHEMA_VERSION):
            self.db.close()
            raise ValueError('Unsupported schema version')
        if version == 0 and self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
            self.db.close()
            raise ValueError('Unrecognized existing database')
        self.db.executescript('''
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=FULL;
        PRAGMA foreign_keys=ON;
        BEGIN;
        CREATE TABLE IF NOT EXISTS evidence(id INTEGER PRIMARY KEY, endpoint TEXT, context TEXT, body TEXT, digest TEXT, observed INTEGER);
        CREATE TABLE IF NOT EXISTS observations(endpoint TEXT, source_id TEXT, digest TEXT, evidence_id INTEGER REFERENCES evidence(id), body TEXT, PRIMARY KEY(endpoint,source_id,digest));
        CREATE TABLE IF NOT EXISTS sightings(endpoint TEXT,source_id TEXT,digest TEXT,evidence_id INTEGER REFERENCES evidence(id),PRIMARY KEY(endpoint,source_id,evidence_id));
        CREATE TABLE IF NOT EXISTS ranges(endpoint TEXT,start INTEGER,stop INTEGER,offset INTEGER,next_offset INTEGER,state TEXT, updated INTEGER, PRIMARY KEY(endpoint,start,stop));
        CREATE TABLE IF NOT EXISTS gaps(id INTEGER PRIMARY KEY, evidence_id INTEGER REFERENCES evidence(id), reason TEXT);
        CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, cycle TEXT, kind TEXT CHECK(kind IN ('plan','decision','review')), body TEXT, recorded INTEGER, effective INTEGER, retrospective INTEGER);
        CREATE TABLE IF NOT EXISTS cycle_aliases(legacy TEXT PRIMARY KEY,stable TEXT);
        CREATE TRIGGER IF NOT EXISTS immutable_evidence_update BEFORE UPDATE ON evidence BEGIN SELECT RAISE(ABORT,'append only'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_evidence_delete BEFORE DELETE ON evidence BEGIN SELECT RAISE(ABORT,'append only'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_observations_update BEFORE UPDATE ON observations BEGIN SELECT RAISE(ABORT,'append only'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_observations_delete BEFORE DELETE ON observations BEGIN SELECT RAISE(ABORT,'append only'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_sightings_update BEFORE UPDATE ON sightings BEGIN SELECT RAISE(ABORT,'append only'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_sightings_delete BEFORE DELETE ON sightings BEGIN SELECT RAISE(ABORT,'append only'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_notes_update BEFORE UPDATE ON notes BEGIN SELECT RAISE(ABORT,'versioned'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_notes_delete BEFORE DELETE ON notes BEGIN SELECT RAISE(ABORT,'versioned'); END;
        INSERT OR IGNORE INTO sightings SELECT endpoint,source_id,digest,evidence_id FROM observations;
        COMMIT;
        ''')
        with self.db:
            if version == 1:
                self._migrate_cycle_aliases()
            self.db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

    def _migrate_cycle_aliases(self):
        # Preserve v1 note references without rewriting immutable notes. Old ordering
        # is replayed solely to find aliases; all new accounting uses numeric IDs.
        fills = sorted(self.records('trades'),
                       key=lambda f: (decimal(str(f.get('create_time', 0))), fill_key(f)))
        net = {}
        index = 0
        for fill in fills:
            try:
                size = decimal(fill['size'])
                contract = fill['contract']
            except (KeyError, ValueError, ArithmeticError):
                continue
            old = net.get(contract, Decimal(0))
            if size and (old == 0 or old * (old + size) < 0):
                legacy = fill_key(fill) + ':' + str(index)
                self.db.execute('INSERT OR IGNORE INTO cycle_aliases VALUES(?,?)',
                                (legacy, cycle_key(fill)))
                index += 1
            net[contract] = old + size

    def ingest(self, endpoint, page, context):
        body = encode(page)
        digest = hashlib.sha256(body.encode()).hexdigest()
        records = page.get('data', page) if endpoint == 'snapshot' else page['data']
        if not isinstance(records, list):
            records = [records]
        with self.db:
            eid = self.db.execute('INSERT INTO evidence(endpoint,context,body,digest,observed) VALUES(?,?,?,?,?)',
                                  (endpoint, encode(context), body, digest, int(time.time()))).lastrowid
            for index, record in enumerate(records):
                source = None
                if isinstance(record, dict):
                    source = record.get('trade_id', record.get('id')) if endpoint == 'trades' else record.get('id')
                if not isinstance(source, (str, int)) or isinstance(source, bool) or str(source) == '':
                    if endpoint in ('account', 'positions', 'snapshot'):
                        source = f'snapshot:{eid}:{index}'
                    else:
                        self._gap(eid, 'missing stable source id; excluded from accounting')
                        continue
                if endpoint == 'trades':
                    if not record.get('contract'):
                        self._gap(eid, 'missing contract; excluded from accounting')
                        continue
                    source = fill_key(record)
                if endpoint in ('trades', 'account-book'):
                    fields = ('size', 'price', 'fee', 'point_fee', 'close_size') if endpoint == 'trades' else ('change', 'balance')
                    try:
                        for field in fields:
                            if field in record:
                                decimal(record[field])
                        if endpoint == 'trades':
                            decimal(str(record['create_time']))
                            decimal(record['size'])
                            decimal(record['price'])
                        else:
                            decimal(record['change'])
                            if 'time' in record:
                                decimal(str(record['time']))
                    except (KeyError, ValueError, ArithmeticError, TypeError):
                        self._gap(eid, 'invalid or floating monetary source; excluded from accounting')
                        continue
                raw = encode(record)
                h = hashlib.sha256(raw.encode()).hexdigest()
                existing = self.db.execute('SELECT digest FROM observations WHERE endpoint=? AND source_id=?',
                                           (endpoint, str(source))).fetchall()
                if endpoint in ('trades', 'account-book') and existing and all(x['digest'] != h for x in existing):
                    self._gap(eid, 'immutable source id conflict; accounting quarantined')
                self.db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)', (endpoint, str(source), h, eid, raw))
                self.db.execute('INSERT INTO sightings VALUES(?,?,?,?)', (endpoint, str(source), h, eid))
            if 'start' in context:
                nxt = page['pagination']['next_offset']
                self.db.execute('INSERT OR REPLACE INTO ranges VALUES(?,?,?,?,?,?,?)',
                                (endpoint, context['start'], context['stop'], context['offset'], nxt,
                                 'complete' if nxt is None else 'partial', int(time.time())))

    def _gap(self, evidence_id, reason):
        self.db.execute('INSERT INTO gaps(evidence_id,reason) VALUES(?,?)', (evidence_id, reason))

    def records(self, endpoint):
        rows = self.db.execute('''SELECT source_id,body,COUNT(*) OVER(PARTITION BY source_id) versions
            FROM observations WHERE endpoint=? ORDER BY evidence_id''', (endpoint,)).fetchall()
        if endpoint not in ('trades', 'account-book'):
            rows = self.db.execute('''SELECT o.source_id,o.body,1 versions FROM sightings s
                JOIN observations o USING(endpoint,source_id,digest)
                WHERE s.endpoint=? ORDER BY s.evidence_id''', (endpoint,)).fetchall()
        latest = {}
        for row in rows:
            if endpoint in ('trades', 'account-book') and row['versions'] != 1:
                continue
            latest[row['source_id']] = json.loads(row['body'])
        return list(latest.values())

    def sync(self, client, endpoint, start, end):
        from gate_reader.client import MAX_WINDOW
        if endpoint not in HISTORICAL_ENDPOINTS or isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int) or not 0 <= start <= end:
            raise ValueError('Invalid sync range')
        cursor = start
        while True:
            stop = min(end, cursor + MAX_WINDOW)
            row = self.db.execute('SELECT * FROM ranges WHERE endpoint=? AND start=? AND stop=?',
                                  (endpoint, cursor, stop)).fetchone()
            if not row:
                with self.db:
                    self.db.execute('INSERT INTO ranges VALUES(?,?,?,?,?,?,?)',
                                    (endpoint, cursor, stop, 0, 0, 'partial', int(time.time())))
            if not row or row['state'] != 'complete':
                offset = row['next_offset'] if row else 0
                for page in client.pages(endpoint, limit=100, offset=offset, **{'from': cursor, 'to': stop}):
                    self.ingest(endpoint, page, {'start': cursor, 'stop': stop, 'offset': offset})
                    offset = page['pagination']['next_offset']
            if stop == end:
                return
            cursor = stop

    def note(self, cycle, kind, body, effective):
        if kind not in ('plan', 'decision', 'review') or not isinstance(body, str) or not body.strip() or len(body) > 10000:
            raise ValueError('Invalid note')
        if cycle not in {c['id'] for c in self.view()['cycles']}:
            raise ValueError('Unknown cycle')
        if not isinstance(effective, int) or isinstance(effective, bool):
            raise ValueError('Invalid note time')
        # This API edits existing trades only. Never label a backfilled record as prior planning.
        with self.db:
            self.db.execute('INSERT INTO notes(cycle,kind,body,recorded,effective,retrospective) VALUES(?,?,?,?,?,1)',
                            (cycle, kind, body, int(time.time()), effective))

    def view(self):
        with localcontext() as ctx:
            # Fixed 60 digits can round large finite cash values. Expand precision
            # to preserve the full integer/fractional span and guard allocations.
            values = [decimal(v) for endpoint in ('trades', 'account-book') for record in self.records(endpoint)
                      for key, v in record.items() if key in ('size', 'price', 'fee', 'point_fee', 'close_size', 'change', 'balance')]
            span = max((v.adjusted() for v in values), default=0) - min((v.as_tuple().exponent for v in values), default=0)
            ctx.prec = max(60, 2 * max(0, span) + 40)
            return self._view()

    def _view(self):
        cycles, active, unallocated = [], {}, []
        accounts = self.records('account')
        dual = bool(accounts and accounts[-1].get('in_dual_mode') is True)
        fills = sorted(self.records('trades'),
                       key=lambda f: (decimal(str(f.get('create_time', 0))), source_order(trade_identifier(f)), f['contract']))
        first = {}
        for fill in fills:
            first.setdefault(fill['contract'], fill)
        unknown_contracts = {contract for contract, fill in first.items() if decimal(fill.get('close_size', 0)) != 0}
        if dual or unknown_contracts:
            valid = []
            for fill in fills:
                if dual or fill['contract'] in unknown_contracts:
                    unallocated.append({'reason': 'dual mode unsupported' if dual else 'prior position history unknown; cycle inference withheld', 'source': fill})
                else:
                    valid.append(fill)
            fills = valid
        for fill in fills:
            size, price, contract = decimal(fill['size']), decimal(fill['price']), fill['contract']
            if not size:
                unallocated.append({'reason': 'zero-size fill', 'source': fill})
                continue
            c = active.get(contract)
            remaining = size
            allocated_fee = Decimal(0)
            while remaining:
                if c is None:
                    c = {'id': cycle_key(fill), 'contract': contract, 'opened': fill['create_time'],
                         'closed': None, 'size': Decimal(0), 'entry': Decimal(0), 'fills': [], 'fees': [],
                         'opening_history_unknown': False, 'plan': '未知：无历史事前计划'}
                    cycles.append(c)
                    active[contract] = c
                old = c['size']
                part = remaining if old == 0 or old * remaining > 0 else (max(remaining, -old) if remaining < 0 else min(remaining, -old))
                if old == 0 or old * part > 0:
                    c['entry'] = (abs(old) * c['entry'] + abs(part) * price) / (abs(old) + abs(part))
                c['fills'].append({'source': fill, 'allocated_size': str(part), 'closing_size': str(abs(part) if old * part < 0 else 0)})
                remaining -= part
                if 'fee' in fill:
                    total_fee = decimal(fill['fee'])
                    fee = total_fee - allocated_fee if remaining == 0 else total_fee * abs(part / size)
                    allocated_fee += fee
                    c['fees'].append({'source_id': fill_key(fill), 'amount': str(fee), 'currency': fill.get('fee_currency', 'unknown')})
                else:
                    unallocated.append({'reason': 'fill fee unavailable', 'source_id': fill_key(fill)})
                c['size'] += part
                if c['size'] == 0:
                    c['closed'] = fill['create_time']
                    active.pop(contract, None)
                    c = None
        order_versions = [dict(o) for o in self.db.execute('''SELECT s.endpoint,s.source_id,o.body,s.evidence_id FROM sightings s
            JOIN observations o USING(endpoint,source_id,digest) WHERE s.endpoint IN ('orders','order-history') ORDER BY s.evidence_id''')]
        for c in cycles:
            c['size'], c['entry'] = str(c['size']), str(c['entry'])
            c['notes'] = [dict(n) for n in self.db.execute('''SELECT * FROM notes WHERE cycle=? OR cycle IN
                (SELECT legacy FROM cycle_aliases WHERE stable=?) ORDER BY id''', (c['id'], c['id']))]
            ids = {str(f['source'].get('order_id')) for f in c['fills']}
            c['order_versions'] = [o for o in order_versions if o['source_id'] in ids]
            current = {}
            for o in c['order_versions']:
                current[o['source_id']] = json.loads(o['body'])
            c['orders'] = list(current.values())
            c['flows'] = []
            c['realised_components'] = {k: Decimal(0) for k in ('pnl', 'fee', 'fund')}
        flows = self.records('account-book')
        categories = {k: Decimal(0) for k in ('dnw', 'pnl', 'fee', 'fund', 'other')}
        for flow in flows:
            amount = decimal(flow['change'])
            kind = flow.get('type')
            categories[kind if kind in categories else 'other'] += amount
            if kind == 'dnw':
                continue
            matches = []
            if kind in ('pnl', 'fee') and flow.get('trade_id') is not None:
                for c in cycles:
                    for f in c['fills']:
                        if trade_identifier(f['source']) == str(flow['trade_id']) and c['contract'] == flow.get('contract'):
                            weight = abs(decimal(f['closing_size'] if kind == 'pnl' else f['allocated_size']))
                            if weight:
                                matches.append((c, weight))
            elif kind == 'fund' and flow.get('time') is not None:
                candidates = [c for c in cycles if c['contract'] == flow.get('contract')
                              and decimal(str(c['opened'])) <= decimal(str(flow['time']))
                              and (c['closed'] is None or decimal(str(flow['time'])) <= decimal(str(c['closed'])))]
                if len(candidates) == 1:
                    matches = [(candidates[0], Decimal(1))]
            total = sum((weight for _, weight in matches), Decimal(0))
            if not total:
                unallocated.append({'reason': 'flow attribution unknown or ambiguous', 'source': flow})
                continue
            allocated = Decimal(0)
            for index, (c, weight) in enumerate(matches):
                part = amount - allocated if index == len(matches) - 1 else amount * weight / total
                allocated += part
                c['realised_components'][kind] += part
                c['flows'].append({'source': flow, 'allocated_change': str(part)})
        for c in cycles:
            c['realised_pnl'] = str(c['realised_components']['pnl']) if c['flows'] else None
            c['trading_cash_net'] = str(sum(c['realised_components'].values(), Decimal(0))) if c['flows'] else None
            c['realised'] = c['trading_cash_net']  # v1 compatibility; UI uses the explicit names.
            c['realised_components'] = {k: str(v) for k, v in c['realised_components'].items()}
        bridge = {'status': 'unknown', 'reason': 'missing balance or timestamp observations'}
        if flows and all('time' in flow for flow in flows):
            ordered = sorted(flows, key=lambda flow: (decimal(str(flow['time'])), source_order(flow.get('id', ''))))
            anchors = [index for index, flow in enumerate(ordered) if 'balance' in flow]
            if anchors:
                first, last = anchors[0], anchors[-1]
                opening = decimal(ordered[first]['balance']) - decimal(ordered[first]['change'])
                ending = decimal(ordered[last]['balance'])
                # Missing intermediate balances do not remove their cash changes.
                changes = sum((decimal(flow['change']) for flow in ordered[first:last + 1]), Decimal(0))
                residual = ending - opening - changes
                outside = first + len(ordered) - last - 1
                status = 'gap' if residual != 0 else ('partial' if outside else 'balanced')
                bridge = {'status': status, 'opening_inferred': str(opening), 'ending_observed': str(ending),
                          'changes': str(changes), 'residual': str(residual), 'flows_outside_anchors': outside,
                          'warning': '仅为已读流水数值桥接；期初余额推导、同秒排序和上游语义仍待核验，零残差不证明完整'}
        ranges = [dict(r) for r in self.db.execute('SELECT * FROM ranges')]
        covered = coverage(ranges)
        cutoffs = {e: r['contiguous_to'] for e, r in covered.items()}
        as_of = min(cutoffs.values()) if all(v is not None for v in cutoffs.values()) else None
        gaps = [dict(g) for g in self.db.execute('SELECT * FROM gaps')]
        reasons = ['上游历史边界包含规则未确认']
        if any(r['status'] != 'read_complete' for r in covered.values()):
            reasons.append('分页缺口或尚未完成全部历史端点；数值仅为已读取数据小计')
        if gaps:
            reasons.append('来源缺失、无效金额或冲突事件已隔离')
        if unallocated:
            reasons.append('存在未分配成交、费用或流水')
        if dual or unknown_contracts:
            reasons.append('双向模式或区间前持仓历史未知；相关周期推导已停止')
        attached_notes = {n['id'] for c in cycles for n in c['notes']}
        orphan_notes = [dict(n) for n in self.db.execute('SELECT * FROM notes ORDER BY id') if n['id'] not in attached_notes]
        contexts=[json.loads(r[0]) for r in self.db.execute('SELECT context FROM evidence')]
        synthetic=bool(contexts) and all(c.get('synthetic') is True for c in contexts)
        return {'synthetic':synthetic, 'cycles': cycles, 'unallocated': unallocated, 'ranges': ranges, 'coverage': covered, 'gaps': gaps,
                'as_of': as_of, 'observed_at': self.db.execute('SELECT MAX(observed) FROM evidence').fetchone()[0],
                'history_cutoffs': cutoffs, 'metrics_status': 'provisional', 'metrics_reasons': reasons,
                'completeness': '暂计：' + '；'.join(reasons), 'orphan_notes': orphan_notes,
                'currency': accounts[-1].get('currency', 'unknown') if accounts else 'unknown',
                'account': accounts, 'positions': self.records('positions'),
                'cash_flow_sum': str(sum((decimal(f['change']) for f in flows), Decimal(0))),
                'balance_bridge': bridge, 'cash_categories': {k: str(v) for k, v in categories.items()},
                'realised_pnl': str(categories['pnl']),
                'period_trading_cash_net': str(sum((categories[k] for k in ('pnl', 'fee', 'fund')), Decimal(0))),
                'deposits': str(sum((decimal(f['change']) for f in flows if f.get('type') == 'dnw' and decimal(f['change']) > 0), Decimal(0))),
                'withdrawals': str(sum((decimal(f['change']) for f in flows if f.get('type') == 'dnw' and decimal(f['change']) < 0), Decimal(0))),
                'unrealised': '仅展示账户/仓位来源快照，与已实现成交盈亏和交易净现金流分开'}

    def backup(self, target):
        target = private_path(target)
        if target.exists():
            raise ValueError('Backup cannot overwrite existing file')
        fd, name = tempfile.mkstemp(dir=target.parent)
        os.close(fd)
        tmp = Path(name)
        try:
            destination = sqlite3.connect(tmp)
            try:
                self.db.backup(destination)
                verify_database(destination)
            finally:
                destination.close()
            with tmp.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else hashlib.sha256(stream.read()).hexdigest()
                os.fsync(stream.fileno())
            # Atomic publication refuses a destination created during backup.
            os.link(tmp, target)
            fsync_directory(target.parent)
            return digest
        finally:
            tmp.unlink(missing_ok=True)
            fsync_directory(target.parent)


def restore(source, target, expected_sha256):
    """Validate exactly the captured bytes and atomically publish a new private DB."""
    source, target = private_path(source), private_path(target)
    if target.exists():
        raise ValueError('Restore cannot overwrite existing file')
    content = source.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError('Backup checksum mismatch')
    fd, name = tempfile.mkstemp(dir=target.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        check = sqlite3.connect(tmp.as_uri() + '?mode=ro', uri=True)
        try:
            verify_database(check)
        finally:
            check.close()
        os.link(tmp, target)
        fsync_directory(target.parent)
    finally:
        tmp.unlink(missing_ok=True)
        fsync_directory(target.parent)
