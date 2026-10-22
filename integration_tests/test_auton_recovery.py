"""Real Auton execution and clean-storage restore; explicitly optional dependencies."""
import asyncio
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest

from atropos.actions import ActionFailed, Context
from atropos.auton_adapter import auton_action
from atropos.authorization import AccessPolicy, Grant, OPERATIONS, Resource
from atropos.dispatch import ApprovedBindings, DecisionDispatcher, DispatchAuthorizer, digest
from atropos.redis_store import RedisStore
from atropos.rule_catalog import instantiate
from atropos.rule_lifecycle import Principal, RuleLifecycle
from atropos.rule_lifecycle_store import RedisLifecycleStore
from atropos.rule_repository import RedisRuleRepository
from atropos.service import WorkflowService
from atropos.worker import Worker
from runtime_fixture import Storage, AutonDaemon, DropResponseProxy, wait_for


class AutonRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.daemon=AutonDaemon();self.addCleanup(self.daemon.close)
        self.storage=Storage();self.addCleanup(self.storage.close)
        self.actor=Principal('operator','demo');self.target='demo/server/lab'
        self.resources=[Resource('demo','server','lab')]
        self.grants=[Grant('operator','demo','intervention',OPERATIONS,{('server','lab')},
                           actions={'auton.approved'},providers={'internal'})]
        self.credential_valid=True
        self.connect(self.storage)
        self.first=self.lifecycle.save_draft(self.actor,instantiate(['cpu-high'],name='cpu'),
            rule_kind='intervention',selections=[('server','lab')],expected_revision=None)
        self.facts={'cpu_percent':95};self.result={'decision':'review','matched_rule':'combined'}
        self.report=self.lifecycle.test(self.actor,'cpu',self.first['revision'],
            [{'name':'High CPU','input':self.facts,'expected':self.result}])
        self.active=self.lifecycle.activate(self.actor,'cpu',self.first['revision'],
            test_id=self.report['id'],expected_generation=None)

    def connect(self, storage):
        # Exercise action timeout independently from scheduler-sensitive lease expiry.
        self.store=RedisStore(storage.client,'integration',lease_ms=5000)
        self.store.initialize()
        self.lifecycle=RuleLifecycle(RedisRuleRepository(storage.client,'demo','integration'),
            RedisLifecycleStore(RedisRuleRepository(storage.client,'demo','integration')),self.policy)

    def policy(self):return AccessPolicy(self.grants,self.resources)

    def prepare(self, code, *, uri=None, endpoint='approved', auth=None, timeout=5):
        self.actions={'auton.approved':auton_action(self.target,[uri or self.daemon.origin],endpoint,
            auth=self.daemon.auth if auth is None else auth)}
        self.binding=dict(organization='demo',rule='cpu',revision=self.first['revision'],result=self.result,
            workflow=dict(version=1,name='approved',start='execute',steps={
                'execute':dict(action='auton.approved',timeout=timeout,with_={})}),input={},max_age_seconds=300)
        self.binding['workflow']['steps']['execute'].pop('with_')
        self.binding['workflow']['steps']['execute']['with']={'args':['-c',code]}
        self.bindings=lambda:ApprovedBindings([self.binding],self.actions)
        self.dispatcher=DecisionDispatcher(self.lifecycle,WorkflowService(self.store,self.actions),self.bindings,lambda actor:'fixture-identity')
        self.guard=DispatchAuthorizer(lambda actor:self.lifecycle,self.bindings,
            lambda actor,credential:self.credential_valid and credential=='fixture-identity',self.store.now)

    def submit(self, event='event-1'):
        return self.dispatcher.dispatch(self.actor,'cpu',['server','lab'],self.facts,digest(self.binding),
            expected_generation=self.active['generation'],event_id=event)

    def run_worker(self):
        return asyncio.run(Worker(self.store,self.actions,authorizer=self.guard,require_authorization=True).run_once(0))

    def marker_code(self, *, exit_code=0, delay=0):
        return "from pathlib import Path; import time,sys; p=Path(%r); p.write_text(p.read_text()+'effect\\n' if p.exists() else 'effect\\n'); time.sleep(%r); sys.exit(%r)" % (str(self.daemon.root/'effect.txt'),delay,exit_code)

    def effects(self):
        path=self.daemon.root/'effect.txt';return path.read_text().splitlines() if path.exists() else []

    def test_success_and_deduplicated_receipt_use_canonical_target(self):
        self.prepare(self.marker_code())
        receipt=self.submit();self.assertEqual(self.submit()['id'],receipt['id'])
        self.run_worker();state=self.store.get(receipt['id'])
        self.assertEqual(state['status'],'succeeded',state)
        self.assertEqual(state['target'],self.target)
        self.assertEqual(self.effects(),['effect'])
        self.assertFalse(self.run_worker());self.assertEqual(self.effects(),['effect'])
        job_id=hashlib.sha256((receipt['id']+':execute').encode()).hexdigest()
        self.assertEqual(state['results']['execute']['job_id'],job_id)
        status,job=self.daemon.request('GET','/jobs/approved/'+job_id)
        self.assertEqual(status,200);self.assertEqual(job['return_code'],0)

    def test_auth_refusal_and_revocation_never_run_commands(self):
        self.prepare(self.marker_code(),auth=('operator','invalid-synthetic-password'))
        receipt=self.submit();self.run_worker()
        self.assertEqual(self.store.get(receipt['id'])['status'],'failed')
        self.assertEqual(self.effects(),[])
        self.prepare(self.marker_code());receipt=self.submit('revoked')
        self.credential_valid=False;self.run_worker()
        self.assertEqual(self.store.get(receipt['id'])['status'],'cancelled')
        self.assertEqual(self.effects(),[])

    def test_partial_failure_is_not_automatically_retried(self):
        self.prepare(self.marker_code(exit_code=7))
        receipt=self.submit();self.run_worker()
        self.assertEqual(self.store.get(receipt['id'])['status'],'failed')
        self.assertEqual(self.effects(),['effect'])
        self.assertFalse(self.run_worker());self.assertEqual(self.effects(),['effect'])

    def test_timeout_keeps_uncertainty_and_target_exclusion(self):
        self.prepare(self.marker_code(delay=2),timeout=.5)
        receipt=self.submit();self.run_worker()
        state=self.store.get(receipt['id'])
        self.assertEqual(state['status'],'uncertain',state)
        wait_for(lambda:len(self.effects())==1)
        self.assertEqual(self.storage.client.get(self.store.target_key(self.target)),receipt['id'])
        self.assertFalse(self.run_worker());self.assertEqual(self.effects(),['effect'])

    def test_lost_response_and_clean_restore_preserve_uncertainty_without_replay(self):
        proxy=DropResponseProxy(self.daemon);self.addCleanup(proxy.close)
        code=self.marker_code();self.prepare(code,uri=proxy.origin)
        receipt=self.submit();self.run_worker()
        self.assertEqual(proxy.admitted_status,200)
        self.assertEqual(proxy.submissions,1)
        wait_for(lambda:len(self.effects())==1)
        state=self.store.get(receipt['id']);self.assertEqual(state['status'],'uncertain',state)
        audit=self.lifecycle.audit(self.actor,'cpu')
        document=deepcopy(self.first['document']);document['checks']['cpu-high']['gte']=99
        second=self.lifecycle.save_draft(self.actor,document,rule_kind='intervention',selections=[('server','lab')],
            expected_revision=self.first['revision'])
        holder=tempfile.TemporaryDirectory();self.addCleanup(holder.cleanup)
        snapshot=self.storage.snapshot(Path(holder.name)/'backup')
        self.storage.close()  # No surviving source database or writable connection.
        restored=Storage(snapshot);self.addCleanup(restored.close)
        self.connect(restored);self.prepare(code,uri=proxy.origin)
        self.assertEqual(self.store.get(receipt['id']),state)
        self.assertEqual(self.lifecycle.active(self.actor,'cpu'),self.active)
        self.assertEqual(self.lifecycle.repository.head('cpu'),second['revision'])
        self.assertEqual(self.lifecycle.test_report(self.actor,'cpu',self.first['revision'],self.report['id']),self.report)
        self.assertEqual(self.lifecycle.audit(self.actor,'cpu'),audit)
        self.assertEqual(restored.client.get(self.store.target_key(self.target)),receipt['id'])
        self.assertEqual(self.store.queue_status()['uncertain_runs'],1)
        self.assertEqual(self.submit()['id'],receipt['id'])
        self.assertFalse(self.run_worker());self.assertEqual(proxy.submissions,1);self.assertEqual(self.effects(),['effect'])
        with self.assertRaises(PermissionError):self.lifecycle.read(Principal('intruder','demo'),'cpu',self.first['revision'])
        # Reconcile only after independently checking the real daemon's final result.
        job_id=hashlib.sha256((receipt['id']+':execute').encode()).hexdigest()
        _,job=self.daemon.request('GET','/jobs/approved/'+job_id)
        self.assertEqual(job['status'],'complete');self.assertEqual(job['return_code'],0)
        self.store.resolve(receipt['id'],'succeeded')
        self.assertIsNone(restored.client.get(self.store.target_key(self.target)))
        self.assertEqual(self.store.queue_status()['uncertain_runs'],0)
        self.assertFalse(self.run_worker());self.assertEqual(self.effects(),['effect'])
        data=(snapshot/'dump.rdb').read_bytes();(snapshot/'dump.rdb').write_bytes(data+b'corrupt')
        with self.assertRaisesRegex(ValueError,'backup_integrity_failed'):Storage(snapshot)
