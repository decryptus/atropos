"""Shared admission budget, including deferred and uncertain work."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from contextlib import redirect_stdout
import unittest
from unittest.mock import patch

from atropos.actions import builtin_actions
from atropos.redis_store import RedisStore, QueueFull, DEFAULT_QUEUE_LIMIT
from atropos.service import WorkflowService
from atropos.test_execution import test_namespace
from atropos.worker import Worker
from support import RedisFixture, workflow


class QueueCapacityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.fixture=RedisFixture()
    @classmethod
    def tearDownClass(cls): cls.fixture.close()

    def setUp(self):
        self.fixture.client.flushdb()
        self.store=RedisStore(self.fixture.client,lease_ms=300)
        self.store.initialize();self.store.configure_queue(1)
        self.actions=builtin_actions()
        self.service=WorkflowService(self.store,self.actions)

    def submit(self,key='first',definition=None):
        return self.service.submit(definition or workflow(),{},'target-'+key,key)

    def step(self):
        asyncio.run(Worker(self.store,self.actions).run_once(0))

    def test_default_limit_and_status_are_read_only(self):
        other=RedisStore(self.fixture.client,'fresh')
        self.assertEqual(other.queue_status()['max_outstanding'],DEFAULT_QUEUE_LIMIT)
        self.assertEqual(other.queue_status()['outstanding'],0)
        self.assertEqual(list(self.fixture.client.scan_iter('fresh:*')),[])

    def test_limit_rejects_new_work_and_preserves_duplicate_receipts(self):
        first=self.submit()
        with self.assertRaises(QueueFull):self.submit('second')
        self.assertEqual(self.submit()['id'],first['id'])
        self.assertEqual(self.store.queue_status()['outstanding'],1)
        self.assertEqual(len(list(self.fixture.client.scan_iter('atropos:run:*'))),1)

    def test_concurrent_producers_share_limit_without_over_admission(self):
        self.store.configure_queue(5)
        def attempt(i):
            service=WorkflowService(RedisStore(self.fixture.client),self.actions)
            try:return service.submit(workflow(),{},'target-'+str(i),'event-'+str(i))['id']
            except QueueFull:return None
        with ThreadPoolExecutor(max_workers=12) as pool:
            results=list(pool.map(attempt,range(32)))
        self.assertEqual(len([r for r in results if r]),5)
        self.assertEqual(self.store.queue_status()['outstanding'],5)
        self.assertEqual(len(list(self.fixture.client.scan_iter('atropos:run:*'))),5)

    def test_running_work_counts_and_completion_releases_capacity(self):
        state=self.submit();message,_=self.store.receive('consumer',block_ms=0)
        claim=self.store.claim(message[0],state['id'],'consumer')
        self.assertEqual(self.store.queue_status()['outstanding'],1)
        with self.assertRaises(QueueFull):self.submit('second')
        result=dict(claim.state,status='succeeded')
        self.assertTrue(self.store.finish(claim,result))
        self.assertEqual(self.store.queue_status()['outstanding'],0)
        self.assertEqual(self.store.get(state['id'])['status'],'succeeded')
        self.submit('second')

    def test_delayed_work_and_promotion_keep_the_same_budget(self):
        state=self.submit();message,_=self.store.receive('consumer',block_ms=0)
        claim=self.store.claim(message[0],state['id'],'consumer')
        self.store.finish(claim,dict(claim.state,status='retrying'),delay=100)
        status=self.store.queue_status()
        self.assertEqual((status['pending_entries'],status['delayed_entries'],status['outstanding']),(0,1,1))
        with self.assertRaises(QueueFull):self.submit('second')
        self.fixture.client.zadd(self.store.delayed,{state['id']:0})
        self.store.promote()
        self.assertEqual(self.store.queue_status()['outstanding'],1)

    def test_expired_lease_quarantine_holds_capacity_until_explicit_resolution(self):
        state=self.submit();message,_=self.store.receive('consumer',block_ms=0)
        self.store.claim(message[0],state['id'],'consumer')
        self.fixture.client.delete(self.store.lease(state['id']))
        self.assertIsNone(self.store.claim(message[0],state['id'],'recovery'))
        status=self.store.queue_status()
        self.assertEqual((status['uncertain_runs'],status['outstanding']),(1,1))
        with self.assertRaises(QueueFull):self.submit('second')
        self.assertEqual(self.fixture.client.get(self.store.target_key(state['target'])),state['id'])
        self.store.resolve(state['id'],'failed')
        self.assertEqual(self.store.queue_status()['outstanding'],0)
        self.submit('second')

    def test_pause_or_reduction_does_not_stop_accepted_work_or_erase_history(self):
        first=self.submit(definition=workflow({'first':{'action':'core.echo','next':'last'},
                                               'last':{'action':'core.echo'}}))
        self.store.configure_queue(0)
        self.assertEqual(self.submit(definition=first['workflow'])['id'],first['id'])
        with self.assertRaises(QueueFull):self.submit('second')
        self.step();self.step()
        self.assertEqual(self.store.get(first['id'])['status'],'succeeded')
        self.assertTrue(self.store.queue_status()['full'])
        self.assertEqual(self.store.queue_status()['outstanding'],0)

    def test_full_queue_does_not_consume_proof_or_append_admission_audit(self):
        self.submit();self.fixture.client.set('admission-proof','unchanged')
        with self.assertRaises(QueueFull):
            self.store.enqueue(workflow(),{},'other','rejected',conditions={'admission-proof':'unchanged'},
                consume=['admission-proof'],audit=('audit',{'operation':'dispatch'}))
        self.assertEqual(self.fixture.client.get('admission-proof'),'unchanged')
        self.assertFalse(self.fixture.client.exists('audit'))
        self.assertFalse(self.fixture.client.exists(self.store.key(hashlib.sha256(b'rejected').hexdigest())))

    def test_configuration_and_counts_survive_storage_restart(self):
        self.submit();self.fixture.restart()
        other=RedisStore(self.fixture.client)
        self.assertEqual(other.queue_status()['max_outstanding'],1)
        with self.assertRaises(QueueFull):WorkflowService(other,self.actions).submit(workflow(),{},'second')
        self.assertEqual(other.queue_status()['outstanding'],1)

    def test_test_and_real_namespaces_have_independent_limits(self):
        self.store.configure_queue(0)
        tests=RedisStore(self.fixture.client,test_namespace('atropos'));tests.configure_queue(2)
        WorkflowService(tests,self.actions).submit(workflow(),{},'synthetic')
        self.assertEqual(tests.queue_status()['outstanding'],1)
        self.assertEqual(self.store.queue_status()['outstanding'],0)
        with self.assertRaises(QueueFull):self.submit()

    def test_invalid_limits_and_corrupt_configuration_fail_closed(self):
        for value in (-1,True,None,1.5,1000001,'2'):
            with self.subTest(value=value),self.assertRaises(ValueError):self.store.configure_queue(value)
        self.assertEqual(self.store.queue_status()['max_outstanding'],1)
        self.fixture.client.set(self.store.capacity,'broken')
        with self.assertRaises(ValueError):self.submit()
        self.assertEqual(self.fixture.client.xlen(self.store.stream),0)

    def test_cli_inspects_and_configures_only_explicit_namespace(self):
        from atropos.cli import main
        env={'ATROPOS_STORAGE_BACKEND':'redis','ATROPOS_STORAGE_URL':'redis://127.0.0.1:%s/0'%self.fixture.port}
        with patch.dict(os.environ,env,clear=True):
            output=io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(['--namespace','ops','queue-configure','--test','--max-outstanding','3']),0)
            data=json.loads(output.getvalue())
            self.assertEqual(data['namespace'],test_namespace('ops'))
            self.assertEqual(data['max_outstanding'],3)
            self.assertEqual(RedisStore(self.fixture.client,'ops').queue_status()['max_outstanding'],DEFAULT_QUEUE_LIMIT)
