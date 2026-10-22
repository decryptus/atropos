import unittest
from unittest.mock import patch
import test_dispatch as helpers
from atropos.authorization import Grant,OPERATIONS
from atropos.definitions import encode
from atropos.intervention_search import InterventionQuery,search
from atropos.rule_lifecycle import Principal
from atropos.test_execution import RuleTestRunner,test_namespace
from atropos.redis_store import RedisStore
from atropos.service import WorkflowService

class InterventionSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.fixture=helpers.RedisFixture()
    @classmethod
    def tearDownClass(cls):cls.fixture.close()

    def setUp(self):
        self.base=helpers.DispatchTests();self.base.fixture=self.fixture;self.base.setUp()
        b=self.base
        test_store=RedisStore(self.fixture.client,test_namespace('atropos'));test_store.initialize()
        b.dispatcher.tests=RuleTestRunner(b.dispatcher,WorkflowService(test_store,b.actions))

    def search(self,**kw):return search(self.base.dispatcher,self.base.actor,'cpu',InterventionQuery(**kw))

    def test_all_authorized_events_before_paging_and_filters(self):
        b=self.base
        for i in range(105):b.submit(event_id='event-'+str(i).zfill(3))
        result=self.search(limit=50,page=3)
        self.assertEqual(result['total'],105);self.assertEqual(len(result['items']),5)
        self.assertEqual(self.search(query='EVENT-104')['total'],1)
        self.assertEqual(self.search(target='CONTAINER/WEB',workflow='TEST',status='queued')['total'],105)
        self.assertEqual(self.search(query='.*')['total'],0)
        self.assertEqual(self.search(until=0)['total'],0)
        self.assertEqual(self.search(since=0)['total'],105)
        self.assertFalse(any('input' in row or 'results' in row for row in result['items']))
        self.assertEqual(b.calls,[])

    def test_scope_owner_identity_and_final_revocation(self):
        b=self.base;b.submit()
        b.grants.append(Grant('bob','org','intervention',OPERATIONS,{('container','web')},actions={'core.echo'}))
        self.assertEqual(search(b.dispatcher,Principal('bob','org'),'cpu',InterventionQuery())['total'],0)
        b.grants=[]
        with patch.object(b.lifecycle.store,'admission_history') as history,self.assertRaises(PermissionError):self.search()
        history.assert_not_called()
        b.setUp();b.submit();original=b.store.get
        def revoke(run):
            state=original(run);b.grants=[];return state
        with patch.object(b.store,'get',side_effect=revoke),self.assertRaises(PermissionError):self.search()
        b.identity=False
        with self.assertRaises(PermissionError):self.search()

    def test_test_and_real_are_separate_and_permission_gated(self):
        b=self.base;b.submit()
        receipt=b.dispatcher.tests.submit(b.actor,'cpu',['container','web'],b.facts,helpers.digest(b.binding),'test-1')
        result=self.search();self.assertEqual(result['total'],2)
        self.assertEqual(self.search(mode='test')['items'][0]['id'],receipt['id'])
        self.assertEqual(self.search(mode='real')['total'],1)
        b.grants=[Grant('alice','org','intervention',{'read'},{('container','web')})]
        self.assertEqual(self.search()['total'],1)
        self.assertEqual(self.search(mode='test')['total'],0)

    def test_missing_authorized_run_and_capacity_never_become_empty(self):
        b=self.base;receipt=b.submit();b.store.client.delete(b.store.key(receipt['id']))
        with self.assertRaises(KeyError):self.search()
        with patch('atropos.intervention_search.MAX_EVENTS',0),self.assertRaises(RuntimeError):self.search()

    def test_malformed_filters_and_test_evidence(self):
        for values in ({'page':True},{'limit':201},{'since':-1},{'since':2,'until':1},{'query':'x\n'},{'status':'all'}):
            with self.subTest(values=values),self.assertRaises(ValueError):InterventionQuery(**values)
        b=self.base;receipt=b.dispatcher.tests.submit(b.actor,'cpu',['container','web'],b.facts,helpers.digest(b.binding),'test-1')
        store=b.dispatcher.tests.workflows.store;state=store.get(receipt['id']);state['test_context']['test']=1
        store.client.set(store.key(receipt['id']),encode(state))
        with self.assertRaises(ValueError):self.search()

    def test_redis_history_pages_are_not_a_total_capacity_limit(self):
        self.base.submit()
        with patch('atropos.intervention_search.MAX_EVENTS',1):
            self.assertEqual(self.search()['total'],1)
