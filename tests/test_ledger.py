import json
import tempfile
import unittest
from pathlib import Path
from review.ledger import Ledger
from gate_reader.client import Client, ReaderError


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'ledger.db';self.l=Ledger(self.path)
    def tearDown(self):
        self.l.db.close();self.tmp.cleanup()
    def page(self, records): return {'data':records,'pagination':{'next_offset':None}}
    def fill(self,id,size,fee='-0.1'): return {'trade_id':id,'size':size,'price':'10.123456789012345678','contract':'DEMO_USDT','fee':fee,'fee_currency':'USDT','create_time':int(id)}
    def test_duplicate_and_order_versions(self):
        p=self.page([self.fill('1','2')]);self.l.ingest('trades',p,{});self.l.ingest('trades',p,{})
        self.assertEqual(len(self.l.records('trades')),1);self.assertEqual(self.l.db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],2)
        for left in ('2','1','2'):self.l.ingest('order-history',self.page([{'id':'o','left':left}]),{})
        self.assertEqual(self.l.records('order-history')[0]['left'],'2');self.assertEqual(self.l.db.execute("SELECT COUNT(*) FROM observations WHERE endpoint='order-history'").fetchone()[0],2)
    def test_partial_close_reversal_fee_conservation(self):
        self.l.ingest('trades',self.page([self.fill('1','3','-0.3'),self.fill('2','-1','-0.1'),self.fill('3','-4','-0.4'),self.fill('4','2','-0.2')]),{})
        v=self.l.view();self.assertEqual(len(v['cycles']),2);self.assertTrue(all(c['closed'] for c in v['cycles']))
        from decimal import Decimal
        self.assertEqual(sum(Decimal(f['amount']) for c in v['cycles'] for f in c['fees']),Decimal('-1'))
        self.assertEqual(v['cycles'][0]['entry'],'10.123456789012345678')
    def test_unknown_identity_conflicts_and_append_only(self):
        self.l.ingest('trades',self.page([{'price':'1'},self.fill('1','2')]),{})
        self.l.ingest('trades',self.page([self.fill('1','3')]),{})
        self.assertEqual(self.l.records('trades'),[]);self.assertEqual(len(self.l.view()['gaps']),2)
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):self.l.db.execute("UPDATE evidence SET body='{}'")
    def test_interruption_resume_and_boundary(self):
        calls=[]
        class Fake(Client):
            failed=False
            def query(_,endpoint,**params):
                calls.append((params['from'],params['to'],params['offset']))
                if params['offset']==100 and not _.failed:
                    _.failed=True;raise ReaderError('interrupted')
                return {'data':[self.fill(str(1+params['offset']),'1')], 'pagination':{'next_offset':100 if params['offset']==0 else None}}
        f=Fake()
        with self.assertRaises(ReaderError):self.l.sync(f,'trades',0,604801)
        self.l.sync(f,'trades',0,604801)
        self.assertEqual(calls[:3],[(0,604800,0),(0,604800,100),(0,604800,100)])
        self.assertIn((604800,604801,0),calls);self.assertEqual(len(self.l.records('trades')),2)
    def test_transaction_rollback(self):
        with self.assertRaises(KeyError):self.l.ingest('trades',self.page([self.fill('1','1')]),{'start':0,'stop':1})
        self.assertEqual(self.l.db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0],0)
    def test_backup_restore_and_notes(self):
        self.l.ingest('trades',self.page([self.fill('1','1')]),{})
        cycle=self.l.view()['cycles'][0]['id'];self.l.note(cycle,'plan','first',1);self.l.note(cycle,'plan','revision',1)
        backup=Path(self.tmp.name)/'backup.db';self.assertEqual(len(self.l.backup(backup)),64)
        restored=Ledger(backup);self.assertEqual(self.l.view(),restored.view());self.assertEqual(restored.db.execute('PRAGMA integrity_check').fetchone()[0],'ok');restored.db.close()
        self.assertTrue(all(n['retrospective'] for n in self.l.view()['cycles'][0]['notes']))
    def test_repository_data_refused(self):
        with self.assertRaises(ValueError):Ledger(Path(__file__).resolve().parents[1]/'private.db')


class AccountingTests(unittest.TestCase):
    setUp=LedgerTests.setUp
    tearDown=LedgerTests.tearDown
    page=LedgerTests.page
    fill=LedgerTests.fill
    def test_cash_authority_no_double_fees_and_balance_bridge(self):
        self.l.ingest('trades',self.page([self.fill('1','1','-0.1'),self.fill('2','-1','-0.2')]),{})
        flows=[{'id':'10','trade_id':'1','contract':'DEMO_USDT','type':'fee','time':1,'change':'-0.1','balance':'99.9'}, {'id':'11','trade_id':'2','contract':'DEMO_USDT','type':'pnl','time':2,'change':'2.123456789012345678','balance':'102.023456789012345678'}, {'id':'12','trade_id':'2','contract':'DEMO_USDT','type':'fee','time':2,'change':'-0.2','balance':'101.823456789012345678'}]
        for _ in range(2):self.l.ingest('account-book',self.page(flows),{})
        v=self.l.view();self.assertEqual(v['cycles'][0]['realised'],'1.823456789012345678');self.assertEqual(v['balance_bridge']['residual'],'0E-18');self.assertEqual(v['cash_categories']['fee'],'-0.3')
    def test_unknown_fee_and_monetary_float(self):
        self.l.ingest('account-book',self.page([{'id':'1','trade_id':'missing','type':'fee','change':'-0.2'}]),{})
        self.assertEqual(len(self.l.view()['unallocated']),1)
        from review.ledger import decimal
        with self.assertRaises(ValueError):decimal(0.1)
    def test_bridge_gap_is_visible(self):
        self.l.ingest('account-book',self.page([{'id':'1','time':1,'type':'dnw','change':'10','balance':'10'},{'id':'2','time':2,'type':'dnw','change':'5','balance':'20'}]),{})
        self.assertEqual(self.l.view()['balance_bridge']['status'],'gap')

    def test_restore_checksum_and_refuse_overwrite(self):
        from review.ledger import restore
        backup=Path(self.tmp.name)/'backup.db';digest=self.l.backup(backup);target=Path(self.tmp.name)/'restored.db'
        with self.assertRaises(ValueError):restore(backup,target,'bad')
        restore(backup,target,digest)
        with self.assertRaises(ValueError):restore(backup,target,digest)
        restored=Ledger(target);self.assertEqual(restored.view(),self.l.view());restored.db.close()

    def test_data_cutoff_does_not_follow_import_time(self):
        for endpoint in ('trades','account-book','order-history'):
            self.l.ingest(endpoint,self.page([]),{'start':1,'stop':2,'offset':0})
        self.assertEqual(self.l.view()['as_of'],2)
        self.assertGreater(self.l.view()['observed_at'],2)
    def test_dual_mode_is_not_inferred_as_net_cycles(self):
        self.l.ingest('account',{'data':{'in_dual_mode':True}},{})
        self.l.ingest('trades',self.page([self.fill('1','1')]),{})
        self.assertEqual(self.l.view()['cycles'],[])
        self.assertEqual(len(self.l.view()['unallocated']),1)

class AuditRegressionTests(unittest.TestCase):
    setUp = LedgerTests.setUp
    tearDown = LedgerTests.tearDown
    page = LedgerTests.page
    fill = LedgerTests.fill

    def test_numeric_ids_order_same_second_fills(self):
        buy = dict(self.fill('2', '1'), create_time=1)
        sell = dict(self.fill('10', '-1'), create_time=1)
        self.l.ingest('trades', self.page([sell, buy]), {})
        cycle = self.l.view()['cycles'][0]
        self.assertEqual(cycle['fills'][0]['source']['trade_id'], '2')
        self.assertEqual(cycle['fills'][1]['source']['trade_id'], '10')
        self.assertEqual(cycle['closed'], 1)

    def test_backfill_does_not_rekey_note_cycle(self):
        self.l.ingest('trades', self.page([self.fill('10', '1'), self.fill('11', '-1')]), {})
        key = self.l.view()['cycles'][0]['id']
        self.l.note(key, 'plan', 'synthetic note', 10)
        self.l.ingest('trades', self.page([dict(self.fill('1', '1'), contract='OTHER_USDT')]), {})
        cycle = next(c for c in self.l.view()['cycles'] if c['contract'] == 'DEMO_USDT')
        self.assertEqual(cycle['id'], key)
        self.assertEqual(cycle['notes'][0]['body'], 'synthetic note')

    def test_string_zero_close_size_is_not_unknown_history(self):
        self.l.ingest('trades', self.page([dict(self.fill('1', '1'), close_size='0')]), {})
        cycle = self.l.view()['cycles'][0]
        self.assertFalse(cycle['opening_history_unknown'])

    def test_unknown_prior_position_withholds_inference(self):
        self.l.ingest('trades', self.page([dict(self.fill('1', '-1'), close_size='1'), self.fill('2', '1')]), {})
        view = self.l.view()
        self.assertEqual(view['cycles'], [])
        self.assertEqual(len(view['unallocated']), 2)
        self.assertTrue(any('区间前持仓' in reason for reason in view['metrics_reasons']))

    def test_partial_early_window_does_not_advance_cutoff(self):
        for endpoint in ('trades', 'account-book', 'order-history'):
            self.l.ingest(endpoint, {'data': [], 'pagination': {'next_offset': 100}}, {'start': 0, 'stop': 10, 'offset': 0})
            self.l.ingest(endpoint, self.page([]), {'start': 10, 'stop': 20, 'offset': 0})
        view = self.l.view()
        self.assertIsNone(view['as_of'])
        self.assertEqual(view['metrics_status'], 'provisional')
        self.assertEqual(view['coverage']['trades']['missing'], [[0, 10]])
        for endpoint in ('trades', 'account-book', 'order-history'):
            self.l.ingest(endpoint, self.page([]), {'start': 0, 'stop': 10, 'offset': 100})
        self.assertEqual(self.l.view()['as_of'], 20)

    def test_fill_id_fallback_preserves_source_and_cash_matching(self):
        source = {'id': 'x', 'contract': 'DEMO_USDT', 'price': '1', 'size': '1', 'fee': '-0.1', 'create_time': 1}
        self.l.ingest('trades', self.page([source]), {})
        self.l.ingest('account-book', self.page([{'id': '1', 'trade_id': 'x', 'contract': 'DEMO_USDT', 'type': 'fee', 'change': '-0.1'}]), {})
        self.assertEqual(self.l.records('trades'), [source])
        self.assertEqual(self.l.view()['cycles'][0]['trading_cash_net'], '-0.1')

    def test_v1_legacy_notes_migrate_without_rewriting(self):
        self.l.ingest('trades', self.page([self.fill('1', '1'), self.fill('2', '-1')]), {})
        with self.l.db:
            self.l.db.execute('INSERT INTO notes(cycle,kind,body,recorded,effective,retrospective) VALUES(?,?,?,?,?,?)', ('DEMO_USDT:1:0', 'plan', 'legacy', 3, 1, 1))
            self.l.db.execute('PRAGMA user_version=1')
        self.l.db.close()
        self.l = Ledger(self.path)
        self.assertEqual(self.l.view()['cycles'][0]['notes'][0]['body'], 'legacy')
        self.assertEqual(self.l.db.execute('SELECT cycle FROM notes').fetchone()[0], 'DEMO_USDT:1:0')
        self.assertEqual(self.l.db.execute('PRAGMA user_version').fetchone()[0], 2)

    def test_large_finite_cash_preserves_all_digits(self):
        amount = '1' + '0' * 70 + '.12345678901234567890123456789'
        increment = '.00000000000000000000000000001'
        self.l.ingest('account-book', self.page([{'id': '1', 'type': 'dnw', 'change': amount}, {'id': '2', 'type': 'dnw', 'change': increment}]), {})
        from decimal import Decimal
        expected = '1' + '0' * 70 + '.12345678901234567890123456790'
        self.assertEqual(Decimal(self.l.view()['cash_categories']['dnw']), Decimal(expected))

    def test_reversal_cash_fee_remainder_and_ambiguous_funding(self):
        self.l.ingest('trades', self.page([self.fill('1', '1'), self.fill('2', '-3'), self.fill('3', '2')]), {})
        self.l.ingest('account-book', self.page([
            {'id': '1', 'trade_id': '2', 'contract': 'DEMO_USDT', 'type': 'fee', 'change': '-0.1', 'time': 2},
            {'id': '2', 'trade_id': '2', 'contract': 'DEMO_USDT', 'type': 'pnl', 'change': '2', 'time': 2},
            {'id': '3', 'contract': 'DEMO_USDT', 'type': 'fund', 'change': '-0.01', 'time': 2},
        ]), {})
        from decimal import Decimal, localcontext
        view = self.l.view()
        with localcontext() as context:
            context.prec = 120
            self.assertEqual(sum(Decimal(c['realised_components']['fee']) for c in view['cycles']), Decimal('-0.1'))
        self.assertEqual(view['cycles'][0]['realised_components']['pnl'], '2')
        self.assertEqual(view['cycles'][1]['realised_components']['pnl'], '0')
        self.assertTrue(any(u.get('source', {}).get('type') == 'fund' for u in view['unallocated']))

    def test_existing_wide_permissions_are_refused_not_changed(self):
        from unittest.mock import patch
        import os
        import stat
        original = Path.stat
        def unsafe(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == self.path:
                values = list(info)
                values[0] = stat.S_IFREG | 0o644
                return os.stat_result(values)
            return info
        with patch.object(Path, 'stat', unsafe):
            with self.assertRaisesRegex(ValueError, 'owner-only'):
                Ledger(self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_backup_publication_race_cannot_overwrite(self):
        from unittest.mock import patch
        import os
        target = Path(self.tmp.name) / 'raced.db'
        link = os.link
        def concurrent_create(source, destination):
            destination.write_bytes(b'concurrent synthetic file')
            return link(source, destination)
        with patch('review.ledger.os.link', concurrent_create):
            with self.assertRaises(FileExistsError):
                self.l.backup(target)
        self.assertEqual(target.read_bytes(), b'concurrent synthetic file')
        self.assertEqual(set(Path(self.tmp.name).iterdir()), {self.path, target, Path(str(self.path) + ".lock")})

    def test_restore_validates_captured_bytes_not_reopened_source(self):
        from unittest.mock import patch
        from review.ledger import restore
        backup = Path(self.tmp.name) / 'backup.db'
        digest = self.l.backup(backup)
        target = Path(self.tmp.name) / 'restored.db'
        read_bytes = Path.read_bytes
        def replace_after_capture(path):
            content = read_bytes(path)
            if path == backup:
                path.write_bytes(b'changed after capture')
            return content
        with patch.object(Path, 'read_bytes', replace_after_capture):
            restore(backup, target, digest)
        restored = Ledger(target)
        self.assertEqual(restored.view(), self.l.view())
        restored.db.close()

    def test_restore_rejects_logical_evidence_tamper(self):
        import sqlite3
        import hashlib
        from review.ledger import restore
        self.l.ingest('trades', self.page([self.fill('1', '1')]), {})
        backup = Path(self.tmp.name) / 'backup.db'
        self.l.backup(backup)
        connection = sqlite3.connect(backup)
        connection.execute('DROP TRIGGER immutable_evidence_update')
        connection.execute("UPDATE evidence SET body='{}'")
        connection.commit()
        connection.close()
        target = Path(self.tmp.name) / 'rejected.db'
        with self.assertRaisesRegex(ValueError, 'digest'):
            restore(backup, target, hashlib.sha256(backup.read_bytes()).hexdigest())
        self.assertFalse(target.exists())

    def test_bad_book_timestamp_is_quarantined(self):
        self.l.ingest('account-book', self.page([{'id': '1', 'type': 'dnw', 'time': 'bad', 'change': '10', 'balance': '10'}]), {})
        self.assertEqual(self.l.records('account-book'), [])
        self.assertEqual(len(self.l.view()['gaps']), 1)

    def test_bridge_includes_intermediate_flow_without_balance(self):
        self.l.ingest('account-book', self.page([
            {'id': '1', 'type': 'dnw', 'time': 1, 'change': '10', 'balance': '10'},
            {'id': '2', 'type': 'dnw', 'time': 2, 'change': '5'},
            {'id': '3', 'type': 'dnw', 'time': 3, 'change': '0', 'balance': '10'},
        ]), {})
        bridge = self.l.view()['balance_bridge']
        self.assertEqual(bridge['status'], 'gap')
        self.assertEqual(bridge['changes'], '15')
        self.assertEqual(bridge['residual'], '-5')

    def test_bridge_missing_time_is_unknown(self):
        self.l.ingest('account-book', self.page([
            {'id': '1', 'type': 'dnw', 'time': 1, 'change': '10', 'balance': '10'},
            {'id': '2', 'type': 'dnw', 'change': '5'},
        ]), {})
        self.assertEqual(self.l.view()['balance_bridge']['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
