import asyncio
import copy
import unittest
from unittest.mock import Mock

from atropos.actions import Action, Context, builtin_actions
from atropos.authorization import AccessPolicy, Grant, OPERATIONS
from atropos.dispatch import ApprovedBindings, DecisionDispatcher, digest
from atropos.redis_store import RedisStore
from atropos.service import WorkflowService
from atropos.test_execution import RuleTestRunner, TestRunAuthorizer, test_namespace
from atropos.worker import Worker
import test_dispatch as setup_helpers


class EndToEndTestModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.fixture=setup_helpers.RedisFixture()
    @classmethod
    def tearDownClass(cls):cls.fixture.close()

    def setUp(self):
        self.base=setup_helpers.DispatchTests();self.base.fixture=self.fixture;self.base.setUp()
        b=self.base
        self.production=[]
        async def forbidden(params,context):
            self.production.append(params);raise AssertionError('real action must never run')
        self.simulations=[]
        async def simulated(params,context):
            self.simulations.append(context)
            return {'status':'completed','_test_context':{'test':False}}
        self.actions=builtin_actions();self.actions['core.echo']=Action(forbidden,True,simulated)
        b.binding['workflow']={'version':1,'name':'intervene-then-alert','start':'intervene','steps':{
            'intervene':{'action':'core.echo','next':'notify'},
            'notify':{'action':'core.alert','with':{'subject':'Intervention completed','status':{'$ref':'/results/intervene/status'}}}}}
        self.grants=[Grant('alice','org','intervention',{'read','test'},{('container','web')},actions={'core.echo','core.alert'})]
        b.lifecycle.policy_loader=lambda:AccessPolicy(self.grants,b.resources)
        self.store=RedisStore(self.fixture.client,test_namespace('atropos'));self.store.initialize()
        self.bindings=lambda:ApprovedBindings([b.binding],self.actions)
        d=DecisionDispatcher(b.lifecycle,WorkflowService(b.store,self.actions),self.bindings,b.credential)
        self.runner=RuleTestRunner(d,WorkflowService(self.store,self.actions))
        self.guard=TestRunAuthorizer(lambda actor:b.lifecycle,self.bindings,lambda actor,cred:b.identity,self.store.now)

    def submit(self):
        b=self.base
        return self.runner.submit(b.actor,'cpu',['container','web'],b.facts,digest(b.binding),'admin-test')
    def step(self,test_mode=True):
        asyncio.run(Worker(self.store,self.actions,authorizer=self.guard,require_authorization=True,test_mode=test_mode).run_once(0))

    def test_test_only_user_runs_simulated_intervention_then_labelled_alert(self):
        b=self.base;receipt=self.submit();self.step();self.step()
        state=self.store.get(receipt['id']);context=receipt['test_context']
        self.assertEqual(state['status'],'succeeded');self.assertEqual(self.production,[])
        self.assertTrue(self.simulations[0].test_mode);self.assertEqual(self.simulations[0].test_id,context['id'])
        self.assertEqual(state['execution']['test_event']['test_context'],context)
        self.assertEqual(state['execution']['test_event']['decision']['test_context'],context)
        for event in state['history']:self.assertEqual(event['test_context'],context)
        for result in state['results'].values():self.assertEqual(result['_test_context'],context)
        alert=state['results']['notify']['alert']
        self.assertEqual(alert['test_context'],context);self.assertTrue(alert['subject'].startswith('[TEST]'))
        self.assertEqual(alert['delivery'],'simulated')
        self.assertEqual(b.store.client.xlen(b.store.stream),0)
        self.assertFalse(b.store.client.exists(b.store.target_key(state['target'])))
        self.assertEqual(self.runner.status(b.actor,'cpu','admin-test')['test_context'],context)

    def test_unguarded_or_real_worker_cannot_execute_test_run(self):
        receipt=self.submit();self.step(False)
        self.assertEqual(self.store.get(receipt['id'])['status'],'cancelled');self.assertEqual(self.production,[])

    def test_missing_simulator_fails_without_fallback(self):
        self.actions['core.echo']=Action(self.actions['core.echo'].execute,True)
        receipt=self.submit();self.step()
        self.assertEqual(self.store.get(receipt['id'])['status'],'cancelled');self.assertEqual(self.production,[])

    def test_removing_test_flag_cannot_promote_run_to_real_execution(self):
        from atropos.definitions import encode
        receipt=self.submit();state=self.store.get(receipt['id']);del state['test_context']
        self.store.client.set(self.store.key(receipt['id']),encode(state))
        self.step(False)
        self.assertEqual(self.store.get(receipt['id'])['status'],'cancelled');self.assertEqual(self.production,[])

    def test_test_permission_revocation_stops_following_steps(self):
        receipt=self.submit();self.step();self.grants=[];self.step()
        state=self.store.get(receipt['id']);self.assertEqual(state['status'],'cancelled')
        self.assertNotIn('notify',state['results']);self.assertEqual(self.production,[])

    def test_duplicate_test_event_has_one_run_and_one_audit(self):
        first=self.submit();second=self.submit();self.assertEqual(first['id'],second['id'])
        self.assertEqual(self.store.client.xlen(self.store.stream),1)
        audit=self.base.lifecycle.audit(self.base.actor,'cpu')
        self.assertEqual(len([e for e in audit if e['operation']=='test_run']),1)

    def test_nonmatching_event_and_preview_never_enqueue(self):
        self.base.facts={'cpu_percent':1}
        receipt=self.submit();self.assertEqual(receipt['status'],'not_matched')
        self.assertTrue(receipt['test_context']['test']);self.assertEqual(self.store.client.xlen(self.store.stream),0)
        self.assertEqual(self.base.store.client.xlen(self.base.store.stream),0)

    def test_simulation_does_not_produce_activation_evidence_or_require_execute(self):
        b=self.base;before=b.lifecycle.store.latest_test('cpu',b.revision)
        preview=self.runner.dispatcher.simulate(b.actor,'cpu',['container','web'],b.facts,digest(b.binding))
        self.assertTrue(preview['simulation']);self.assertTrue(preview['matches_binding'])
        self.assertEqual(b.lifecycle.store.latest_test('cpu',b.revision),before)
        self.assertEqual(self.production,[]);self.assertEqual(self.store.client.xlen(self.store.stream),0)

    def test_action_test_scope_is_required(self):
        self.grants=[Grant('alice','org','intervention',{'read','test'},{('container','web')})]
        with self.assertRaises(PermissionError):self.submit()
        self.assertEqual(self.store.client.xlen(self.store.stream),0)

    def test_auton_rejects_test_context_before_creating_service(self):
        from atropos.auton_adapter import auton_action
        from atropos.actions import ActionFailed
        factory=Mock();action=auton_action('target',['https://example.invalid'],'noop',service_factory=factory)
        context=Context('run','step','target',1,10,True,'test-id')
        with self.assertRaises(ActionFailed):asyncio.run(action.execute({},context))
        factory.assert_not_called()

    def test_simulated_failure_follows_error_branch_and_keeps_test_alert_marker(self):
        self.base.binding['workflow']={'version':1,'name':'failed-and-alert','start':'intervene','steps':{
            'intervene':{'action':'core.fail','on_error':'notify'},
            'notify':{'action':'core.alert','with':{'subject':'Intervention failed'}}}}
        self.grants=[Grant('alice','org','intervention',{'read','test'},{('container','web')},actions={'core.fail','core.alert'})]
        receipt=self.submit();self.step();self.step();state=self.store.get(receipt['id'])
        self.assertEqual(state['status'],'succeeded')
        self.assertEqual(state['history'][1]['error'],'action_failed')
        self.assertEqual(state['results']['notify']['alert']['test_context'],receipt['test_context'])
        self.assertEqual(self.production,[])

    def test_binding_discovery_is_scoped_metadata_without_queue_or_inputs(self):
        b=self.base
        rows=self.runner.bindings(b.actor,'cpu',['container','web'])
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0],{'id':digest(b.binding),'rule':'cpu','revision':b.revision,
            'workflow':'intervene-then-alert','actions':['core.alert','core.echo'],'test_supported':True})
        self.assertEqual(self.store.client.xlen(self.store.stream),0)
        self.assertEqual(b.store.client.xlen(b.store.stream),0)
        self.assertEqual(self.production,[])
        self.assertNotIn('input',rows[0]);self.assertNotIn('organization',rows[0])

    def test_binding_discovery_excludes_wrong_actor_organization_rule_and_target(self):
        from atropos.rule_lifecycle import Principal
        b=self.base
        for actor,rule,target in [(Principal('bob','org'),'cpu',['container','web']),
                (Principal('alice','other'),'cpu',['container','web']),
                (b.actor,'other',['container','web']),(b.actor,'cpu',['server','host'])]:
            with self.subTest(actor=actor,rule=rule,target=target):
                self.assertEqual(self.runner.bindings(actor,rule,target),[])

    def test_binding_discovery_requires_read_test_and_every_action_scope(self):
        for operations,actions in [({'read'}, {'core.echo','core.alert'}),
                ({'test'}, {'core.echo','core.alert'}),({'read','test'},{'core.echo'})]:
            self.grants=[Grant('alice','org','intervention',operations,{('container','web')},actions=actions)]
            with self.subTest(operations=operations,actions=actions):
                self.assertEqual(self.runner.bindings(self.base.actor,'cpu',['container','web']),[])

    def test_binding_discovery_group_grant_does_not_grant_server(self):
        self.grants=[Grant('alice','org','intervention',{'read','test'},{('container_group','apps')},
                           actions={'core.echo','core.alert'})]
        self.assertEqual(len(self.runner.bindings(self.base.actor,'cpu',['container','web'])),1)
        self.assertEqual(self.runner.bindings(self.base.actor,'cpu',['server','host']),[])

    def test_discovery_reports_missing_simulator_without_real_fallback(self):
        self.runner.workflows.actions['core.echo']=Action(self.actions['core.echo'].execute,True)
        rows=self.runner.bindings(self.base.actor,'cpu',['container','web'])
        self.assertFalse(rows[0]['test_supported']);self.assertEqual(self.production,[])

    def test_discovery_does_not_authorize_later_submission_after_revocation(self):
        self.assertEqual(len(self.runner.bindings(self.base.actor,'cpu',['container','web'])),1)
        self.grants=[]
        with self.assertRaises(PermissionError):self.submit()
        self.assertEqual(self.runner.bindings(self.base.actor,'cpu',['container','web']),[])
        self.assertEqual(self.store.client.xlen(self.store.stream),0)

    def test_binding_discovery_rejects_bad_target_shape(self):
        for target in ([], ['container'], ['container_group','apps'], ['container',None]):
            with self.subTest(target=target),self.assertRaises(ValueError):
                self.runner.bindings(self.base.actor,'cpu',target)

    def test_archive_between_simulation_and_admission_refuses_queue_write(self):
        from unittest.mock import patch
        from atropos.redis_store import Conflict
        b=self.base
        self.grants=[Grant('alice','org','intervention',OPERATIONS,{('container','web')},actions={'core.echo','core.alert'})]
        b.lifecycle.deactivate(b.actor,'cpu',expected_generation=b.active['generation'])
        original=self.runner.workflows.submit
        def race(*args,**kwargs):
            b.lifecycle.archive(b.actor,'cpu',expected_revision=b.revision,expected_generation=None)
            return original(*args,**kwargs)
        with patch.object(self.runner.workflows,'submit',side_effect=race):
            with self.assertRaises(Conflict):self.submit()
        self.assertEqual(self.store.queue_status()['outstanding'],0)
        self.assertFalse(any(e['operation']=='test_run' for e in b.lifecycle.audit(b.actor,'cpu')))

    def test_archive_cancels_queued_simulation_and_preserves_its_receipt(self):
        b=self.base
        self.grants=[Grant('alice','org','intervention',OPERATIONS,{('container','web')},actions={'core.echo','core.alert'})]
        receipt=self.submit()
        b.lifecycle.deactivate(b.actor,'cpu',expected_generation=b.active['generation'])
        b.lifecycle.archive(b.actor,'cpu',expected_revision=b.revision,expected_generation=None)
        self.step()
        self.assertEqual(self.runner.status(b.actor,'cpu','admin-test')['status'],'cancelled')
        self.assertEqual(self.simulations,[])
        self.assertEqual(self.runner.bindings(b.actor,'cpu',['container','web']),[])
