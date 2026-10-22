import asyncio
import copy
import unittest
from unittest.mock import patch

from atropos.actions import Action, ActionFailed, OutcomeUnknown, builtin_actions
from atropos.authorization import AccessPolicy, Grant, OPERATIONS, Resource
from atropos.dispatch import ApprovedBindings, DecisionDispatcher, DispatchAuthorizer, digest
from atropos.redis_store import Conflict, RedisStore
from atropos.rule_catalog import instantiate
from atropos.rule_lifecycle import Principal, RuleLifecycle
from atropos.rule_lifecycle_store import RedisLifecycleStore
from atropos.rule_repository import RedisRuleRepository
from atropos.service import WorkflowService
from atropos.worker import Worker
from support import RedisFixture, workflow


class DispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.fixture = RedisFixture()

    @classmethod
    def tearDownClass(cls): cls.fixture.close()

    def setUp(self):
        self.fixture.client.flushdb()
        self.store = RedisStore(self.fixture.client)
        self.store.initialize()
        self.actor = Principal('alice', 'org')
        self.resources = [Resource('org', 'server', 'host'), Resource('org', 'container', 'web', 'host', {'apps'})]
        self.grants = [Grant('alice','org','intervention',OPERATIONS,{('container_group','apps')},
                             actions={'core.echo','core.fail'}, providers={'internal'})]
        self.repository = RedisRuleRepository(self.fixture.client, 'org')
        self.lifecycle = RuleLifecycle(self.repository, RedisLifecycleStore(self.repository), self.policy)
        draft = self.lifecycle.save_draft(self.actor, instantiate(['cpu-high'], name='cpu'),
                    rule_kind='intervention', selections=[('container','web')], expected_revision=None)
        self.revision = draft['revision']
        self.facts = {'cpu_percent':95}
        self.result = {'decision':'review','matched_rule':'combined'}
        report = self.lifecycle.test(self.actor,'cpu',self.revision,[{'name':'hot','input':self.facts,'expected':self.result}])
        self.active = self.lifecycle.activate(self.actor,'cpu',self.revision,test_id=report['id'],expected_generation=None)
        self.actions = builtin_actions()
        self.calls = []
        async def record(params, context):
            self.calls.append((params, context.target))
            return params
        self.actions['core.echo'] = Action(record, True)
        self.definition = workflow({'first':{'action':'core.echo','with':{'value':{'$ref':'/input/value'}},'next':'last'},
                                    'last':{'action':'core.echo','with':{'value':{'$ref':'/results/first/value'}}}})
        self.binding = {'organization':'org','rule':'cpu','revision':self.revision,'result':self.result,
                        'workflow':self.definition,'input':{'value':'approved'},'max_age_seconds':60}
        self.documents = [self.binding]
        self.identity = True
        self.dispatcher = DecisionDispatcher(self.lifecycle, WorkflowService(self.store,self.actions),
                                             self.bindings, self.credential)
        self.guard = DispatchAuthorizer(lambda actor:self.lifecycle, self.bindings,
                                       lambda actor, credential:self.identity and credential=='synthetic', self.store.now)

    def policy(self): return AccessPolicy(self.grants, self.resources)
    def bindings(self): return ApprovedBindings(self.documents,self.actions)
    def credential(self, actor):
        if not self.identity: raise PermissionError()
        return 'synthetic'
    def submit(self, **changes):
        args = dict(actor=self.actor,rule='cpu',target=['container','web'],facts=self.facts,
                    binding_id=digest(self.binding),expected_generation=self.active['generation'],event_id='event-1')
        args.update(changes)
        return self.dispatcher.dispatch(**args)
    def step(self, guarded=True):
        worker = Worker(self.store,self.actions,authorizer=self.guard if guarded else None)
        asyncio.run(worker.run_once(0))
    def deactivate(self):
        self.lifecycle.deactivate(self.actor,'cpu',expected_generation=self.active['generation'])

    def test_plan_is_pure_and_exact_workflow_is_executed(self):
        plan=self.dispatcher.plan(self.actor,'cpu',['container','web'],self.facts,digest(self.binding))
        self.assertEqual(plan['input'],{'value':'approved'})
        self.assertEqual(self.fixture.client.xlen(self.store.stream),0)
        receipt=self.submit()
        self.step();self.step()
        self.assertEqual(self.store.get(receipt['id'])['status'],'succeeded')
        self.assertEqual(self.calls,[({'value':'approved'},'org/container/web')]*2)
        self.assertEqual(self.dispatcher.status(self.actor,'cpu','event-1')['id'],receipt['id'])
        audit=self.lifecycle.audit(self.actor,'cpu')
        self.assertEqual(audit[-1]['operation'],'dispatch')
        self.assertEqual(audit[-1]['run_id'],receipt['id'])

    def test_deduplication_recovers_receipt_without_reenqueue_or_duplicate_audit(self):
        first=self.submit();second=self.submit()
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(self.fixture.client.xlen(self.store.stream),1)
        self.assertEqual(len(self.lifecycle.audit(self.actor,'cpu')),2)
        with self.assertRaises(Conflict):self.submit(facts={'cpu_percent':96})
        self.deactivate()
        self.assertEqual(self.dispatcher.status(self.actor,'cpu','event-1')['id'],first['id'])

    def test_missing_action_permission_denies_before_queue(self):
        self.grants=[Grant('alice','org','intervention',OPERATIONS,{('container_group','apps')},providers={'internal'})]
        with self.assertRaises(PermissionError):self.submit()
        self.assertEqual(self.fixture.client.xlen(self.store.stream),0)

    def test_wrong_decision_target_rule_generation_and_organization_are_denied(self):
        for changes in ({'facts':{'cpu_percent':1}},{'target':['server','host']},
                        {'rule':'other'},{'expected_generation':'0'*32},{'actor':Principal('alice','other')}):
            with self.subTest(changes=changes),self.assertRaises((PermissionError,Conflict)):
                self.submit(**changes)
        self.assertEqual(self.fixture.client.xlen(self.store.stream),0)

    def test_unreviewed_binding_changes_cannot_reuse_approved_id(self):
        original=digest(self.binding)
        self.binding['input']={'value':'changed'}
        with self.assertRaises(PermissionError):self.submit(binding_id=original)
        document=copy.deepcopy(self.binding);document['workflow']['steps']['first']['action']='shell.run'
        with self.assertRaises(ValueError):ApprovedBindings([document],self.actions)

    def test_atomic_generation_check_prevents_enqueue_after_evaluation(self):
        original=self.dispatcher.workflows.submit
        def racing(*args,**kwargs):
            self.deactivate()
            return original(*args,**kwargs)
        with patch.object(self.dispatcher.workflows,'submit',side_effect=racing),self.assertRaises(Conflict):
            self.submit()
        self.assertEqual(self.fixture.client.xlen(self.store.stream),0)
        self.assertNotIn('dispatch',[e['operation'] for e in self.lifecycle.audit(self.actor,'cpu')])

    def test_queued_identity_binding_policy_or_activation_revocation_cancels_without_effect(self):
        for kind in ('identity','binding','policy','activation'):
            with self.subTest(kind=kind):
                self.setUp();receipt=self.submit()
                if kind=='identity':self.identity=False
                elif kind=='binding':self.documents=[]
                elif kind=='policy':self.grants=[]
                else:self.deactivate()
                self.step()
                state=self.store.get(receipt['id'])
                self.assertEqual(state['status'],'cancelled')
                self.assertEqual(self.calls,[])
                self.assertFalse(self.fixture.client.exists(self.store.target_key(state['target'])))

    def test_permission_revocation_between_steps_blocks_next_step(self):
        receipt=self.submit();self.step()
        self.grants=[];self.step()
        state=self.store.get(receipt['id'])
        self.assertEqual(len(self.calls),1)
        self.assertEqual(state['status'],'cancelled')
        self.assertEqual(state['history'][-1]['error'],'execution_authorization_denied')

    def test_expired_decision_is_cancelled(self):
        receipt=self.submit()
        self.guard.now=lambda:self.store.get(receipt['id'])['created_at']+61
        self.step()
        self.assertEqual(self.store.get(receipt['id'])['status'],'cancelled')
        self.assertEqual(self.calls,[])

    def test_guard_failure_or_missing_guard_cannot_execute_attributed_work(self):
        for guard in (None,lambda state:False,lambda state:(_ for _ in ()).throw(OSError('private error'))):
            with self.subTest(guard=guard):
                self.setUp();receipt=self.submit()
                asyncio.run(Worker(self.store,self.actions,authorizer=guard).run_once(0))
                self.assertEqual(self.store.get(receipt['id'])['status'],'cancelled')
                self.assertEqual(self.calls,[])

    def test_tampered_stored_workflow_and_parameters_fail_closed(self):
        for field in ('input','workflow'):
            with self.subTest(field=field):
                self.setUp();receipt=self.submit();state=self.store.get(receipt['id'])
                if field=='input':state[field]['value']='injected'
                else:state[field]['steps']['first']['with']={'value':'injected'}
                from atropos.definitions import encode
                self.fixture.client.set(self.store.key(receipt['id']),encode(state))
                self.step()
                self.assertEqual(self.store.get(receipt['id'])['status'],'cancelled')
                self.assertEqual(self.calls,[])

    def test_uncertain_effect_retains_lock_and_never_retries(self):
        async def unknown(params,context):
            self.calls.append(params);raise OutcomeUnknown()
        self.actions['core.echo']=Action(unknown,True)
        receipt=self.submit();self.step();self.step()
        state=self.store.get(receipt['id'])
        self.assertEqual(state['status'],'uncertain')
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.fixture.client.get(self.store.target_key(state['target'])),receipt['id'])

    def test_revocation_does_not_take_error_branch(self):
        self.binding['workflow']=workflow({'first':{'action':'core.echo','on_error':'fallback'},
                                          'fallback':{'action':'core.echo'}})
        receipt=self.submit();self.identity=False;self.step();self.step()
        self.assertEqual(self.store.get(receipt['id'])['status'],'cancelled')
        self.assertEqual(self.calls,[])

    def test_status_requires_original_identity_and_current_read_permission(self):
        self.submit()
        with self.assertRaises(KeyError):self.dispatcher.status(Principal('bob','org'),'cpu','event-1')
        self.grants=[]
        with self.assertRaises(PermissionError):self.dispatcher.status(self.actor,'cpu','event-1')

    def test_retry_attempt_rechecks_permissions(self):
        import time
        async def failed(params,context):
            self.calls.append(params);raise ActionFailed()
        self.actions['core.echo']=Action(failed,True)
        self.binding['workflow']=workflow({'first':{'action':'core.echo','retry':{'attempts':2,'delay':.01}}})
        receipt=self.submit();self.step()
        self.assertEqual(self.store.get(receipt['id'])['status'],'retrying')
        self.identity=False;time.sleep(.02);self.step()
        self.assertEqual(self.store.get(receipt['id'])['status'],'cancelled')
        self.assertEqual(len(self.calls),1)

    def test_strict_worker_refuses_unattributed_and_legacy_signed_work(self):
        for attribution in (None,{'subject':'alice','organization':'org','issuer':'pilot','workflow_revision':'0'*64}):
            with self.subTest(attribution=attribution):
                self.setUp()
                state=WorkflowService(self.store,self.actions).submit(workflow(),{},'org/container/web',attribution=attribution)
                asyncio.run(Worker(self.store,self.actions,authorizer=self.guard,require_authorization=True).run_once(0))
                self.assertEqual(self.store.get(state['id'])['status'],'cancelled')
                self.assertEqual(self.calls,[])
