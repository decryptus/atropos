"""Archive inspection must be offline, redacted and preserve every byte of evidence."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from atropos.retention import inspect_archive
from atropos.storage_backup import write_backup, capture
from atropos.sqlite_backend import SQLiteBackend
from atropos.service import WorkflowService
from atropos.actions import builtin_actions
from atropos.cli import main
from atropos.test_execution import test_namespace


class RetentionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.backend=SQLiteBackend(dict(backend='sqlite',url='sqlite:///'+str(self.root/'engine.db')))
        self.addCleanup(self.backend.close)
        self.store=self.backend.workflow_store('engine');self.store.initialize()
        self.backend.workflow_store(test_namespace('engine')).initialize()
        self.backend.maintenance('engine').pause()
        self.service=WorkflowService(self.store,builtin_actions())
        self.service.submit(dict(version=1,name='private-workflow',start='first',steps={'first':dict(action='core.echo')}),
                            {'secret':'private-payload'},'private-target','private-dedup')
        self.payload=capture(self.backend,'engine');self.path=self.root/'archive.json';write_backup(self.payload,self.path)

    def test_sql_report_counts_without_opening_storage_and_redacts_payload(self):
        original=self.path.read_bytes();before=self.backend.connection.total_changes
        with patch('socket.create_connection',side_effect=AssertionError('no network')),patch('atropos.cli.open_storage',side_effect=AssertionError('no storage')):
            report=inspect_archive(self.path)
        self.assertEqual(report['real']['statuses']['queued'],1)
        self.assertEqual(report['test']['runs'],0)
        self.assertEqual(report['capacity']['archive_bytes']['used'],len(original))
        self.assertEqual(report['table_rows']['runs'],1)
        self.assertFalse(report['purge_supported']);self.assertFalse(report['restore_verified'])
        for secret in ('private-workflow','private-payload','private-target','private-dedup'):
            self.assertNotIn(secret,json.dumps(report))
        self.assertEqual(self.path.read_bytes(),original);self.assertEqual(self.backend.connection.total_changes,before)

    def test_uncertain_run_lock_and_deduplication_survive_inspection(self):
        import asyncio
        from atropos.actions import Action, OutcomeUnknown
        from atropos.worker import Worker
        async def unknown(params,context):raise OutcomeUnknown()
        self.backend.maintenance('engine').resume(reconciled=True)
        asyncio.run(Worker(self.store,{'core.echo':Action(unknown)}).run_once(0))
        self.backend.maintenance('engine').pause()
        payload=capture(self.backend,'engine');path=self.root/'uncertain.json';write_backup(payload,path)
        before=list(self.backend.connection.execute('SELECT * FROM records ORDER BY "key"'))
        report=inspect_archive(path)
        self.assertEqual(report['real']['statuses']['uncertain'],1)
        self.assertEqual(before,list(self.backend.connection.execute('SELECT * FROM records ORDER BY "key"')))
        self.assertTrue(any(':target:' in row[0] for row in before))
        receipt=self.service.submit(dict(version=1,name='private-workflow',start='first',steps={'first':dict(action='core.echo')}),
                            {'secret':'private-payload'},'private-target','private-dedup')
        self.assertEqual(receipt['status'],'uncertain')
        self.assertEqual(self.backend.connection.execute('SELECT COUNT(*) FROM runs').fetchone()[0],1)

    def test_server_sql_archives_keep_the_same_namespace_and_status_counts(self):
        # Data-only format uses the same row layout; real server capture is covered by SQL CI.
        for backend in ('postgresql','mysql'):
            payload=deepcopy(self.payload);payload.update(backend=backend,schema=1)
            path=self.root/(backend+'.json');write_backup(payload,path)
            report=inspect_archive(path)
            self.assertEqual(report['backend'],backend);self.assertEqual(report['real']['runs'],1)
            self.assertEqual(report['real']['statuses']['queued'],1)

    def test_real_redis_dump_remains_opaque_and_namespaces_are_separate(self):
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
        from support import RedisFixture
        from atropos.redis_maintenance import RedisExecutionMaintenance
        fixture=RedisFixture();self.addCleanup(fixture.close)
        import redis
        client=redis.Redis(host='127.0.0.1',port=fixture.port,decode_responses=False);self.addCleanup(client.close)
        RedisExecutionMaintenance(client,'engine').pause()
        client.set('engine:run:private-id',json.dumps(dict(status='succeeded',secret='private-value')))
        client.set(test_namespace('engine')+':run:private-test',json.dumps(dict(status='failed')))
        path=self.root/'redis.json';write_backup(capture(client,'engine'),path)
        before={key:client.dump(key) for key in client.keys()}
        report=inspect_archive(path)
        self.assertEqual(report['real']['runs'],1);self.assertEqual(report['test']['runs'],1)
        self.assertIsNone(report['real']['statuses'])
        self.assertNotIn('private-',json.dumps(report))
        self.assertEqual(before,{key:client.dump(key) for key in client.keys()})

    def test_capacity_warning_and_cli_exit_codes_do_not_prune(self):
        output=io.StringIO()
        with patch('atropos.retention.MAX_ROWS',sum(len(x) for x in self.payload['tables'].values())),redirect_stdout(output):
            self.assertEqual(main(['storage-retention','--input',str(self.path)]),1)
        report=json.loads(output.getvalue());self.assertTrue(report['attention_required'])
        self.assertEqual(report['capacity']['rows']['remaining'],0)
        with redirect_stdout(io.StringIO()):self.assertEqual(main(['storage-retention','--input',str(self.path)]),0)
        with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            self.assertEqual(main(['storage-retention','--input',str(self.root/'absent.json')]),2)
        self.assertFalse((self.root/'absent.json').exists())

    def test_bad_digest_permissions_symlink_and_unknown_status_fail_closed(self):
        original=self.path.read_bytes();outer=json.loads(original);outer['payload']['tables']['runs'][0][3]='uncertain'
        self.path.write_text(json.dumps(outer))
        with self.assertRaises(ValueError):inspect_archive(self.path)
        self.path.write_bytes(original);self.path.chmod(0o644)
        with self.assertRaises(ValueError):inspect_archive(self.path)
        self.path.chmod(0o600);link=self.root/'link.json';link.symlink_to(self.path)
        with self.assertRaises((ValueError,OSError)):inspect_archive(link)
        bad=deepcopy(self.payload);bad['tables']['runs'][0][3]='not-a-status'
        path=self.root/'bad.json';write_backup(bad,path)
        with self.assertRaises(ValueError):inspect_archive(path)
        self.assertEqual(self.path.read_bytes(),original)
