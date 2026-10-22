"""Persistent admission and execution fencing on actual disposable storage."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import redis
from atropos.actions import Action, builtin_actions
from atropos.maintenance import MaintenanceError
from atropos.redis_maintenance import RedisExecutionMaintenance
from atropos.redis_store import RedisStore, QueueFull
from atropos.storage_backup import BackupError, backup, capture, restore
from atropos.test_execution import test_namespace
from atropos.worker import Worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from support import RedisFixture, workflow


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.source = RedisFixture(); self.addCleanup(self.source.close)
        self.target = RedisFixture(); self.addCleanup(self.target.close)
        self.raw = redis.Redis(host='127.0.0.1', port=self.source.port, decode_responses=False)
        self.dest = redis.Redis(host='127.0.0.1', port=self.target.port, decode_responses=False)
        self.addCleanup(self.raw.close); self.addCleanup(self.dest.close)
        self.store = RedisStore(self.source.client, 'maintenance', lease_ms=300)
        self.store.initialize()
        self.control = RedisExecutionMaintenance(self.raw, 'maintenance')

    def submit(self, key):
        return self.store.enqueue(workflow(), {}, 'demo/server/'+key, key)

    def test_pause_between_receive_and_claim_fences_execution(self):
        run=self.submit('one')
        message,_=self.store.receive('old',block_ms=0)
        self.control.pause()
        self.assertIsNone(self.store.claim(message[0],run['id'],'old'))
        self.assertEqual(self.store.get(run['id'])['attempts'],{})
        self.submit('two')
        self.assertEqual(self.store.queue_status()['outstanding'],2)
        self.assertFalse(asyncio.run(Worker(self.store,builtin_actions()).run_once(0)))
        with self.assertRaises(MaintenanceError):self.control.resume()
        self.control.resume(reconciled=True)
        # Recover the message read just before the pause after its consumer idle time.
        time.sleep(.32)
        worker=Worker(self.store,builtin_actions())
        for _ in range(4):asyncio.run(worker.run_once(0))
        self.assertEqual(self.store.get(run['id'])['status'],'succeeded')
        self.assertEqual(self.store.queue_status()['outstanding'],0)

    def test_pause_finishes_current_step_but_not_the_next(self):
        effects=[]
        async def first(params, context):
            self.control.pause(); effects.append('first')
            with self.assertRaisesRegex(BackupError,'in-flight'):capture(self.raw,'maintenance')
            return {}
        async def second(params,context):effects.append('second');return {}
        actions={'ops.first':Action(first),'ops.second':Action(second)}
        definition=workflow({'first':{'action':'ops.first','next':'second'},'second':{'action':'ops.second'}})
        run=self.store.enqueue(definition,{},'target')
        worker=Worker(self.store,actions)
        asyncio.run(worker.run_once(0));asyncio.run(worker.run_once(0))
        self.assertEqual(effects,['first'])
        state=self.store.get(run['id']);self.assertEqual(state['status'],'queued')
        self.assertEqual(state['current'],'second')
        capture(self.raw,'maintenance')
        self.control.resume(reconciled=True);asyncio.run(worker.run_once(0))
        self.assertEqual(effects,['first','second'])

    def test_restart_restore_retains_both_queues_and_deduplication(self):
        self.store.configure_queue(20);self.control.pause()
        first=self.submit('one');self.submit('two')
        tests=RedisStore(self.source.client,test_namespace('maintenance'));tests.initialize()
        # State/queue storage remains separate; execution markers tested via actual API below.
        test_run=tests.enqueue(workflow(),{},'test-target','one')
        self.source.restart()
        self.assertTrue(self.control.status()['execution_paused'])
        self.assertEqual(self.submit('one')['id'],first['id'])
        payload=capture(self.raw,'maintenance')
        restore(self.dest,'maintenance',payload,source_stopped=True)
        restored=RedisStore(self.target.client,'maintenance');restored.initialize()
        self.assertEqual(restored.queue_status()['max_outstanding'],20)
        self.assertEqual(restored.queue_status()['pending_entries'],2)
        self.assertEqual(RedisStore(self.target.client,test_namespace('maintenance')).get(test_run['id'])['status'],'queued')
        self.assertFalse(asyncio.run(Worker(restored,builtin_actions()).run_once(0)))
        control=RedisExecutionMaintenance(self.dest,'maintenance');control.resume(reconciled=True)
        worker=Worker(restored,builtin_actions())
        asyncio.run(worker.run_once(0));asyncio.run(worker.run_once(0))
        self.assertEqual(restored.get(first['id'])['status'],'succeeded')

    def test_backup_operation_blocks_resume_and_competing_backup_until_write_finishes(self):
        self.submit('one')
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        from atropos.storage_backup import write_backup
        def publishing(payload,path):
            with self.assertRaisesRegex(MaintenanceError,'in_progress'):self.control.resume(reconciled=True)
            with self.assertRaisesRegex(MaintenanceError,'in_progress'):self.control.begin_backup()
            self.submit('later')  # Admission remains available during file publication.
            self.assertFalse(any(e['key'].endswith(':backup-operation') for e in payload['entries']))
            self.assertFalse(any(e['key']==self.store.key(self.submit('later')['id']) for e in payload['entries']))
            return write_backup(payload,path)
        with patch('atropos.storage_backup.write_backup',side_effect=publishing):
            backup(self.raw,'maintenance',Path(temp.name)/'backup.json')
        self.assertEqual(self.store.queue_status()['outstanding'],2)
        self.assertIsNone(self.control.status()['backup_operation'])
        self.assertTrue(self.control.status()['execution_paused'])

    def test_failed_publication_and_crashed_operator_do_not_resume(self):
        with patch('atropos.storage_backup.write_backup',side_effect=OSError('test')):
            with self.assertRaises(OSError):backup(self.raw,'maintenance','unused')
        self.assertTrue(self.control.status()['execution_paused'])
        token=self.control.begin_backup();self.source.restart()
        with self.assertRaises(MaintenanceError):self.control.resume(reconciled=True)
        with self.assertRaises(MaintenanceError):self.control.recover(token)
        with self.assertRaises(MaintenanceError):self.control.recover('0'*32,backup_stopped=True)
        self.control.recover(token,backup_stopped=True)
        self.assertTrue(self.control.status()['execution_paused'])

    def test_concurrent_paused_producers_remain_bounded_without_eviction(self):
        self.store.configure_queue(5);self.control.pause()
        def submit(index):
            try:return self.submit(str(index))['id']
            except QueueFull:return None
        with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(submit,range(20)))
        self.assertEqual(len([v for v in results if v]),5)
        self.assertEqual(self.store.queue_status()['outstanding'],5)
        capture(self.raw,'maintenance')
        self.assertTrue(self.control.status()['execution_paused'])

    def test_expired_running_lease_is_quarantined_while_paused_never_replayed(self):
        run=self.submit('one');message,_=self.store.receive('crashed',block_ms=0)
        self.store.claim(message[0],run['id'],'crashed')
        self.control.pause();time.sleep(.33)
        asyncio.run(Worker(self.store,builtin_actions()).run_once(0))
        self.assertEqual(self.store.get(run['id'])['status'],'uncertain')
        self.assertEqual(self.source.client.get(self.store.target_key(run['target'])),run['id'])
        capture(self.raw,'maintenance')

    def test_delayed_checkpoint_restores_and_waits_for_resume(self):
        run=self.submit('one');message,_=self.store.receive('old',block_ms=0)
        claim=self.store.claim(message[0],run['id'],'old')
        state=dict(claim.state,status='retrying');self.store.finish(claim,state,delay=30)
        self.control.pause();payload=capture(self.raw,'maintenance')
        restore(self.dest,'maintenance',payload,source_stopped=True)
        restored=RedisStore(self.target.client,'maintenance')
        self.assertEqual(restored.queue_status()['delayed_entries'],1)
        self.assertFalse(asyncio.run(Worker(restored,builtin_actions()).run_once(0)))
        self.assertEqual(restored.get(run['id'])['status'],'retrying')
