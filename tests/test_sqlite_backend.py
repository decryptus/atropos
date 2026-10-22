import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from atropos.actions import builtin_actions
from atropos.backend_registry import open_storage, open_queue
from atropos.service import WorkflowService
from atropos.storage_contract import Conflict, QueueFull
from atropos.worker import Worker
from support import RedisFixture, workflow


class SQLiteBackendTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.spec={'backend':'sqlite','url':'sqlite:///'+self.directory.name+'/state.db'}
        self.backend=open_storage(self.spec);self.addCleanup(self.backend.close)
        self.store=self.backend.workflow_store('engine')
        self.actions=builtin_actions()
        self.service=WorkflowService(self.store,self.actions)

    def submit(self, target='host', key=None, **kwargs):
        return self.service.submit(workflow(),{},target,key,**kwargs)

    def step(self):return asyncio.run(Worker(self.store,self.actions).run_once(0))

    def test_reopen_retains_definitions_results_history_and_dedup(self):
        run=self.submit(key='event');self.step()
        other=open_storage(self.spec);self.addCleanup(other.close)
        store=other.workflow_store('engine')
        saved=store.get(run['id'])
        self.assertEqual(saved['status'],'succeeded');self.assertEqual(saved['workflow'],workflow())
        self.assertEqual(len(saved['history']),2)
        self.assertEqual(WorkflowService(store,self.actions).submit(workflow(),{},'host','event'),saved)
        self.assertEqual(store.queue_status()['outstanding'],0)

    def test_timestamp_decimal_projection_keeps_exact_json_constraint(self):
        import sqlite3
        # SQLite 3.45 parses this JSON decimal one ULP away from Python's float.
        timestamp = 1797123232.482005
        with patch.object(self.store, 'now', return_value=timestamp):
            run = self.submit()
        row = self.backend.connection.execute(
            "SELECT created,json_extract(state,'$.created_at') FROM runs WHERE id=?",
            (run['id'],)).fetchone()
        self.assertEqual(row[0], row[1])
        self.assertEqual(self.store.get(run['id'])['created_at'], timestamp)
        # Updates retain the original timestamp and strict metadata constraint.
        self.store._put(dict(run, status='retrying'), self.store._row(run['id'])[1])
        self.assertEqual(self.store.get(run['id'])['created_at'], timestamp)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.backend.transaction():
                self.backend.connection.execute('UPDATE runs SET created=created+1 WHERE id=?', (run['id'],))
        # Snapshot serialization must retain the same checked numeric projection.
        import json
        self.backend.maintenance('engine').pause()
        payload = json.loads(json.dumps(self.backend.capture_backup('engine')))
        restored = open_storage(dict(backend='sqlite', url='sqlite:///'+self.directory.name+'/restored.db'))
        self.addCleanup(restored.close)
        restored.restore_backup('engine', payload)
        self.assertEqual(restored.workflow_store('engine').get(run['id'])['created_at'], timestamp)

    def test_two_connections_admit_once_and_capacity_is_atomic(self):
        self.store.configure_queue(1)
        other=open_storage(self.spec);self.addCleanup(other.close)
        second=WorkflowService(other.workflow_store('engine'),self.actions)
        with ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda service:service.submit(workflow(),{},'host','event'),[self.service,second]))
        self.assertEqual(results[0]['id'],results[1]['id'])
        with self.assertRaises(QueueFull):second.submit(workflow(),{},'other','another')
        self.assertEqual(self.store.queue_status()['outstanding'],1)
        with self.assertRaises(Conflict):second.submit(workflow(),{},'changed','event')

    def test_claim_race_fencing_and_uncertainty_preserve_target_exclusion(self):
        run=self.submit();other=open_storage(self.spec);self.addCleanup(other.close)
        second=other.workflow_store('engine')
        message,_=self.store.receive('one',block_ms=0)
        with ThreadPoolExecutor(2) as pool:
            claims=list(pool.map(lambda store:store.claim(message[0],run['id'],'worker'),[self.store,second]))
        self.assertEqual(sum(c is not None for c in claims),1)
        claim=next(c for c in claims if c)
        with self.backend.transaction():
            self.backend.connection.execute('UPDATE runs SET lease_until=0 WHERE namespace=? AND id=?',('engine',run['id']))
        self.assertFalse(self.store.heartbeat(claim))
        self.assertFalse(self.store.finish(claim,dict(claim.state,status='succeeded')))
        self.step();self.assertEqual(self.store.get(run['id'])['status'],'uncertain')
        next_run=self.submit();self.step()
        self.assertEqual(self.store.get(next_run['id'])['attempts'],{})
        self.assertEqual(self.store.queue_status()['outstanding'],2)
        self.store.resolve(run['id'],'succeeded')
        with patch.object(self.store,'now',return_value=time.time()+1): self.step()
        self.assertEqual(self.store.get(next_run['id'])['status'],'succeeded')

    def test_condition_consumption_audit_and_queue_commit_or_rollback_together(self):
        self.backend.set('proof','present')
        self.store.configure_queue(0)
        arguments=dict(conditions={'proof':'present'},consume=('proof',),audit=('audit',{'event':'admit'}))
        with self.assertRaises(QueueFull):self.submit(**arguments)
        self.assertEqual(self.backend.get('proof'),'present');self.assertEqual(self.backend.entries('audit'),[])
        self.store.configure_queue(1)
        run=self.submit(**arguments)
        self.assertIsNone(self.backend.get('proof'));self.assertEqual(len(self.backend.entries('audit')),1)
        self.assertEqual(self.store.get(run['id'])['status'],'queued')
        with self.assertRaises(Conflict):self.submit(**arguments)

    def test_rollback_after_state_write_preserves_evidence(self):
        self.backend.set('proof','present')
        with patch.object(self.backend,'append',side_effect=RuntimeError('synthetic')),self.assertRaises(RuntimeError):
            self.submit(conditions={'proof':'present'},consume=('proof',),audit=('audit',{}))
        self.assertEqual(self.backend.get('proof'),'present');self.assertEqual(self.store.queue_status()['outstanding'],0)

    def test_pause_and_test_namespace_are_independent_of_admission(self):
        from atropos.test_execution import test_namespace
        control=self.backend.maintenance('engine');control.pause()
        run=self.submit();self.assertFalse(self.step())
        self.assertEqual(self.store.get(run['id'])['attempts'],{})
        test_store=self.backend.workflow_store(test_namespace('engine'))
        self.assertEqual(test_store.queue_status()['outstanding'],0)
        self.assertTrue(control.status()['test_execution_paused'])
        control.resume(reconciled=True);self.step()
        self.assertEqual(self.store.get(run['id'])['status'],'succeeded')

    def test_sqlite_with_redis_delivery_recovers_lost_broker_and_ack_failure(self):
        fixture=RedisFixture();self.addCleanup(fixture.close)
        queue=open_queue({'backend':'redis','url':f'redis://127.0.0.1:{fixture.port}/0'},'engine')
        self.store=self.backend.workflow_store('engine',queue)
        self.service=WorkflowService(self.store,self.actions)
        # Admission persists without any broker access.
        with patch.object(queue,'send',side_effect=RuntimeError('offline')):
            run=self.submit()
            with self.assertRaises(RuntimeError):self.step()
        fixture.client.flushdb()  # Disposable broker loss; outbox reconstructs delivery.
        with patch.object(queue,'acknowledge',side_effect=RuntimeError('lost ack')),self.assertRaises(RuntimeError):self.step()
        self.assertEqual(self.store.get(run['id'])['status'],'succeeded')
        self.assertEqual(self.store.get(run['id'])['attempts'],{'first':1})
        fixture.client.flushdb()
        self.assertFalse(self.step())
        self.assertEqual(self.store.get(run['id'])['attempts'],{'first':1})

    def test_delayed_step_is_not_dispatched_early(self):
        run=self.submit();message,_=self.store.receive('worker',block_ms=0)
        claim=self.store.claim(message[0],run['id'],'worker')
        self.store.finish(claim,dict(claim.state,status='retrying'),delay=60)
        self.assertFalse(self.step())
        with patch.object(self.store,'now',return_value=time.time()+61):self.step()
        self.assertEqual(self.store.get(run['id'])['status'],'succeeded')

    def test_invalid_memory_and_newer_storage_format_rejected(self):
        with self.assertRaises(ValueError):open_storage({'backend':'sqlite','url':'sqlite:///:memory:'})
        self.backend.connection.execute('PRAGMA user_version=99')
        with self.assertRaises(ValueError):open_storage(self.spec)

    def test_redis_storage_accepts_an_independent_delivery_queue(self):
        fixture=RedisFixture();self.addCleanup(fixture.close)
        storage=open_storage({'backend':'redis','url':f'redis://127.0.0.1:{fixture.port}/0'});self.addCleanup(storage.close)
        queue=open_queue({'backend':'redis','url':f'redis://127.0.0.1:{fixture.port}/1'},'separate')
        store=storage.workflow_store('separate',queue);store.initialize()
        run=WorkflowService(store,self.actions).submit(workflow(),{},'host')
        asyncio.run(Worker(store,self.actions).run_once(0))
        self.assertEqual(store.get(run['id'])['status'],'succeeded')
        self.assertEqual(store.queue_status()['outstanding'],0)
        # A stale redelivery cannot claim a later step or terminal run.
        queue.send('1-0',run['id'])
        asyncio.run(Worker(store,self.actions).run_once(0))
        self.assertEqual(store.get(run['id'])['attempts'],{'first':1})

    def test_schema_enforces_sign_lengths_status_and_foreign_keys(self):
        import sqlite3
        run=self.submit()
        self.assertEqual(len(run['id']),64)
        invalid=[('INSERT INTO journal(id,key,value) VALUES(-1,?,?)',('audit','{}')),
                 ('INSERT INTO records(key,value) VALUES(NULL,?)',('{}',)),
                 ('UPDATE runs SET id=? WHERE id=?',('a'*63,run['id'])),
                 ('UPDATE runs SET delivery=? WHERE id=?',('a'*33,run['id'])),
                 ('UPDATE runs SET created=-1 WHERE id=?',(run['id'],)),
                 ('UPDATE runs SET status=? WHERE id=?',('invalid',run['id']))]
        for sql,args in invalid:
            with self.subTest(sql=sql),self.assertRaises(sqlite3.IntegrityError):
                with self.backend.transaction():self.backend.connection.execute(sql,args)
        self.assertEqual(self.store.get(run['id'])['status'],'queued')

    def test_paused_recovery_prioritizes_expired_leases_over_full_ready_batch(self):
        for i in range(100):self.submit(target='queued-'+str(i))
        run=self.submit(target='active')
        claim=self.store.claim(self.store._row(run['id'])[1],run['id'],'worker')
        self.assertIsNotNone(claim)
        self.backend.maintenance('engine').pause()
        with patch.object(self.store,'now',return_value=time.time()+60):
            message,_=self.store.receive('recovery',block_ms=0)
            self.assertEqual(message[1]['run_id'],run['id'])
            self.assertIsNone(self.store.claim(message[0],run['id'],'recovery'))
        self.assertEqual(self.store.get(run['id'])['status'],'uncertain')
        self.assertEqual(self.backend.get(self.store.target_key('active')),run['id'])

    def test_old_schema_is_rejected_without_modifying_existing_data(self):
        self.backend.connection.execute('PRAGMA user_version=1')
        run=self.submit()
        with self.assertRaisesRegex(ValueError,'unsupported_storage_format'):open_storage(self.spec)
        self.assertEqual(self.store.get(run['id'])['status'],'queued')
