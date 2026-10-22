import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

from atropos.actions import builtin_actions
from atropos.backend_registry import open_storage, open_queue
from atropos.maintenance import MaintenanceError
from atropos.storage_backup import backup, capture, read_backup, restore, BackupError, write_backup
from atropos.test_execution import test_namespace
from atropos.worker import Worker
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from support import RedisFixture, workflow


class SQLiteBackupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.backend('source')
        self.target = self.backend('target')
        self.store = self.source.workflow_store('engine')
        self.control = self.source.maintenance('engine')

    def backend(self, name):
        backend = open_storage({'backend': 'sqlite', 'url': 'sqlite:///' + str(self.root / (name + '.db'))})
        self.addCleanup(backend.close)
        return backend

    def submit(self, key):
        return self.store.enqueue(workflow(), {}, key, key)

    def snapshot(self):
        self.control.pause()
        return capture(self.source, 'engine')

    def test_roundtrip_preserves_queue_audit_expiry_sequence_and_deduplication(self):
        self.store.configure_queue(17)
        run = self.submit('one')
        tests = self.source.workflow_store(test_namespace('engine'))
        test_run = tests.enqueue(workflow(), {}, 'test', 'test')
        self.source.append('engine:audit', 'event')
        self.source.set('engine:proof', 'expired', ex=-1)
        self.source.connection.execute("UPDATE sqlite_sequence SET seq=42 WHERE name='journal'")
        path = self.root / 'backup.json'
        backup(self.source, 'engine', path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        restore(self.target, 'engine', read_backup(path), source_stopped=True)
        restored = self.target.workflow_store('engine')
        self.assertEqual(restored.get(run['id']), run)
        self.assertEqual(self.target.workflow_store(test_namespace('engine')).get(test_run['id']), test_run)
        self.assertEqual(restored.queue_status()['max_outstanding'], 17)
        self.assertEqual(self.target.entries('engine:audit'), ['event'])
        self.assertIsNone(self.target.get('engine:proof'))
        self.assertEqual(self.target.append('engine:audit', 'later'), 43)
        self.assertEqual(restored.enqueue(workflow(), {}, 'one', 'one'), run)
        self.assertFalse(asyncio.run(Worker(restored, builtin_actions()).run_once(0)))
        self.target.maintenance('engine').resume(reconciled=True)
        self.assertTrue(asyncio.run(Worker(restored, builtin_actions()).run_once(0)))
        self.assertEqual(restored.get(run['id'])['status'], 'succeeded')

    def test_uncertain_and_delayed_runs_keep_target_exclusion(self):
        uncertain = self.submit('uncertain')
        message, _ = self.store.receive('lost', block_ms=0)
        self.store.claim(message[0], uncertain['id'], 'lost')
        with self.assertRaises(BackupError): self.snapshot()
        self.source.connection.execute('UPDATE runs SET lease_until=0')
        asyncio.run(Worker(self.store, builtin_actions()).run_once(0))
        self.control.resume(reconciled=True)
        delayed = self.submit('delayed')
        message, _ = self.store.receive('delayed', block_ms=0)
        claim = self.store.claim(message[0], delayed['id'], 'delayed')
        self.store.finish(claim, dict(claim.state, status='retrying'), delay=60)
        restore(self.target, 'engine', self.snapshot(), source_stopped=True)
        restored = self.target.workflow_store('engine')
        self.assertEqual(restored.get(uncertain['id'])['status'], 'uncertain')
        self.assertEqual(restored.get(delayed['id'])['status'], 'retrying')
        for run in (uncertain, delayed):
            self.assertEqual(self.target.get(restored.target_key(run['target'])), run['id'])
        self.target.maintenance('engine').resume(reconciled=True)
        self.assertFalse(asyncio.run(Worker(restored, builtin_actions()).run_once(0)))

    def test_fence_survives_restart_and_blocks_resume_during_publication(self):
        self.submit('first')
        def publishing(payload, path):
            other = self.backend('source')
            with self.assertRaises(MaintenanceError): other.maintenance('engine').resume(reconciled=True)
            with self.assertRaises(MaintenanceError): other.maintenance('engine').begin_backup()
            self.submit('later')
            return write_backup(payload, path)
        with patch('atropos.storage_backup.write_backup', side_effect=publishing):
            backup(self.source, 'engine', self.root / 'backup.json')
        payload = read_backup(self.root / 'backup.json')
        self.assertEqual(len(payload['tables']['runs']), 1)
        self.assertEqual(self.store.queue_status()['outstanding'], 2)
        self.assertTrue(self.control.status()['execution_paused'])
        token = self.control.begin_backup()
        other = self.backend('source').maintenance('engine')
        with self.assertRaises(MaintenanceError): other.recover(token)
        with self.assertRaises(MaintenanceError): other.recover('0'*32, backup_stopped=True)
        other.recover(token, backup_stopped=True)
        self.assertTrue(other.status()['execution_paused'])

    def test_failed_publication_leaves_pause_and_releases_only_own_fence(self):
        with patch('atropos.storage_backup.write_backup', side_effect=OSError('failure')):
            with self.assertRaises(OSError): backup(self.source, 'engine', self.root / 'backup.json')
        self.assertTrue(self.control.status()['execution_paused'])
        self.assertIsNone(self.control.status()['backup_operation'])

    def test_invalid_rows_rollback_and_existing_destination_is_untouched(self):
        self.submit('one'); payload = self.snapshot()
        bad = deepcopy(payload); bad['tables']['runs'][0][1] = 'short'
        with self.assertRaises(BackupError): restore(self.target, 'engine', bad, source_stopped=True)
        self.assertEqual(self.target.connection.execute('SELECT COUNT(*) FROM records').fetchone()[0], 0)
        self.target.set('engine:sentinel', 'keep')
        with self.assertRaises(BackupError): restore(self.target, 'engine', payload, source_stopped=True)
        self.assertEqual(self.target.get('engine:sentinel'), 'keep')
        with self.assertRaises(ValueError): restore(self.target, 'engine', payload)

    def test_foreign_namespace_and_tampered_archive_are_rejected(self):
        self.source.set('other:value', 'private')
        with self.assertRaises(ValueError): self.snapshot()
        self.source.delete('other:value')
        path = self.root / 'backup.json'; backup(self.source, 'engine', path)
        path.write_text(path.read_text().replace('"schema":2', '"schema":1'))
        with self.assertRaises(ValueError): read_backup(path)

    def test_redis_delivery_is_rebuilt_after_restore_and_restart(self):
        redis = RedisFixture(); self.addCleanup(redis.close)
        queue_spec = {'backend':'redis', 'url':'redis://127.0.0.1:%s/0' % redis.port}
        run = self.submit('one'); payload = self.snapshot()
        # Any prior delivery namespace, including an empty stream, is rejected.
        old = open_queue(queue_spec, 'engine'); self.addCleanup(old.close)
        old.send('0'*32, run['id'])
        self.target.backup_queue = queue_spec
        with self.assertRaises(BackupError): restore(self.target, 'engine', payload, source_stopped=True)
        self.assertEqual(self.target.connection.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 0)
        # Fresh delivery database, same durable engine namespace.
        fresh = dict(queue_spec, url=queue_spec['url'][:-1] + '1')
        self.target.backup_queue = fresh
        restore(self.target, 'engine', payload, source_stopped=True)
        redis.restart()
        restored = self.target.workflow_store('engine', open_queue(fresh, 'engine'))
        self.assertFalse(asyncio.run(Worker(restored, builtin_actions()).run_once(0)))
        self.target.maintenance('engine').resume(reconciled=True)
        self.assertTrue(asyncio.run(Worker(restored, builtin_actions()).run_once(0)))
        self.assertEqual(restored.get(run['id'])['status'], 'succeeded')
        self.assertFalse(asyncio.run(Worker(restored, builtin_actions()).run_once(0)))

    def test_runnable_archive_and_missing_uncertain_lock_are_rejected(self):
        import json
        self.submit('one'); payload = self.snapshot()
        bad = deepcopy(payload); row = bad['tables']['runs'][0]
        state = json.loads(row[2]); state['status'] = 'running'
        row[2] = json.dumps(state); row[3] = 'running'
        with self.assertRaises(BackupError): restore(self.target, 'engine', bad, source_stopped=True)
        self.assertEqual(self.target.connection.execute('SELECT COUNT(*) FROM records').fetchone()[0], 0)
        state['status'] = 'uncertain'; row[2] = json.dumps(state); row[3] = 'uncertain'
        with self.assertRaises(BackupError): restore(self.target, 'engine', bad, source_stopped=True)
        self.assertEqual(self.target.connection.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 0)
        with patch('atropos.sql_backup.MAX_ROWS', 0):
            with self.assertRaises(ValueError): capture(self.source, 'engine')
