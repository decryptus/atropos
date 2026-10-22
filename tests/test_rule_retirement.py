import copy
import unittest
from concurrent.futures import ThreadPoolExecutor

from atropos.authorization import AccessPolicy, Grant, OPERATIONS, Resource
from atropos.redis_store import Conflict
from atropos.rule_catalog import instantiate
from atropos.rule_lifecycle import Principal, RuleLifecycle
from atropos.rule_lifecycle_store import RedisLifecycleStore
from atropos.rule_repository import RedisRuleRepository
from support import RedisFixture


class RuleRetirementTests(unittest.TestCase):
    def setUp(self):
        self.fixture=RedisFixture();self.addCleanup(self.fixture.close)
        self.actor=Principal('alice','org')
        self.inventory=[Resource('org','server','host'),Resource('org','server','other')]
        self.grants=[Grant('alice','org','intervention',OPERATIONS,
                           frozenset({('server','host'),('server','other')}),providers=frozenset({'internal','download'}))]
        self.repository=RedisRuleRepository(self.fixture.client,'org')
        self.store=RedisLifecycleStore(self.repository)
        self.app=RuleLifecycle(self.repository,self.store,lambda:AccessPolicy(self.grants,self.inventory))
        self.document=instantiate(['cpu-high'],name='cpu')
        self.record=self.save()
        self.args=dict(expected_revision=self.record['revision'],expected_generation=None)

    def save(self,expected=None,target='host'):
        return self.app.save_draft(self.actor,self.document,rule_kind='intervention',
                                   selections=[['server',target]],expected_revision=expected)

    def _tested(self):
        return self.app.test(self.actor,'cpu',self.record['revision'],[{'name':'high','input':{'cpu_percent':95},
                'expected':{'decision':'review','matched_rule':'combined'}}])

    def test_archive_and_unarchive_preserve_history_without_activation(self):
        report=self._tested()
        state=self.app.archive(self.actor,'cpu',**self.args)
        self.assertEqual(state['status'],'archived')
        self.assertEqual(self.app.read(self.actor,'cpu',self.record['revision']),self.record)
        self.assertEqual(self.app.test_report(self.actor,'cpu',self.record['revision'],report['id']),report)
        with self.assertRaises(Conflict):self.save(self.record['revision'])
        with self.assertRaises(Conflict):self._tested()
        with self.assertRaises(Conflict):self.app.activate(self.actor,'cpu',self.record['revision'],
            test_id=report['id'],expected_generation=None)
        result=self.app.unarchive(self.actor,'cpu',expected_revision=self.record['revision'],expected_generation=state['generation'])
        self.assertEqual(result['status'],'available')
        self.assertIsNone(self.app.active(self.actor,'cpu')['revision'])
        self.assertEqual([e['operation'] for e in self.app.audit(self.actor,'cpu')],['archive','unarchive'])

    def test_active_and_previously_active_rules_cannot_be_deleted(self):
        report=self._tested()
        active=self.app.activate(self.actor,'cpu',self.record['revision'],test_id=report['id'],expected_generation=None)
        with self.assertRaisesRegex(Conflict,'deactivate'):self.app.archive(self.actor,'cpu',**self.args)
        with self.assertRaisesRegex(Conflict,'deactivate'):self.app.delete(self.actor,'cpu',**self.args)
        self.app.deactivate(self.actor,'cpu',expected_generation=active['generation'])
        with self.assertRaisesRegex(Conflict,'activated'):self.app.delete(self.actor,'cpu',**self.args)
        self.app.archive(self.actor,'cpu',**self.args)

    def test_draft_delete_removes_contents_and_tests_but_retains_scoped_audit(self):
        self._tested();second=self.save(self.record['revision'])
        archived=self.app.archive(self.actor,'cpu',expected_revision=second['revision'],expected_generation=None)
        result=self.app.delete(self.actor,'cpu',expected_revision=second['revision'],expected_generation=archived['generation'])
        self.assertEqual(result['status'],'deleted')
        self.assertEqual(self.app.availability(self.actor,'cpu')['status'],'deleted')
        self.assertEqual([e['operation'] for e in self.app.audit(self.actor,'cpu')],['archive','delete'])
        self.assertEqual(set(self.fixture.client.keys(self.repository._key('cpu','*'))),
                         {self.repository.availability_key('cpu'),self.store._key('cpu','audit')})
        with self.assertRaises(KeyError):self.app.read(self.actor,'cpu',self.record['revision'])
        with self.assertRaises(Conflict):self.save()
        self.grants=[]
        self.assertEqual(self.app.audit(self.actor,'cpu'),[])
        with self.assertRaises(PermissionError):self.app.availability(self.actor,'cpu')

    def test_linked_test_or_real_intervention_prevents_draft_delete(self):
        from atropos.definitions import encode
        for operation in ('test_run','dispatch'):
            self.fixture.client.delete(self.store._key('cpu','audit'))
            self.fixture.client.rpush(self.store._key('cpu','audit'),encode({'operation':operation}))
            with self.assertRaisesRegex(Conflict,'linked_intervention'):
                self.app.delete(self.actor,'cpu',**self.args)
        self.assertEqual(self.repository.head('cpu'),self.record['revision'])

    def test_permissions_are_distinct_and_cover_every_historical_scope(self):
        second=self.save(self.record['revision'],'other')
        self.grants=[Grant('alice','org','intervention',frozenset({'archive'}),frozenset({('server','other')}))]
        with self.assertRaises(PermissionError):self.app.archive(self.actor,'cpu',expected_revision=second['revision'],expected_generation=None)
        self.grants=[Grant('alice','org','intervention',frozenset({'archive'}),frozenset({('server','host'),('server','other')}))]
        archived=self.app.archive(self.actor,'cpu',expected_revision=second['revision'],expected_generation=None)
        for method in (self.app.unarchive,self.app.delete):
            with self.assertRaises(PermissionError):method(self.actor,'cpu',expected_revision=second['revision'],expected_generation=archived['generation'])
        with self.assertRaises(PermissionError):self.app.availability(Principal('bob','other'),'cpu')

    def test_stale_head_or_availability_generation_never_mutates(self):
        second=self.save(self.record['revision'])
        with self.assertRaises(Conflict):self.app.archive(self.actor,'cpu',**self.args)
        archived=self.app.archive(self.actor,'cpu',expected_revision=second['revision'],expected_generation=None)
        with self.assertRaises(Conflict):self.app.unarchive(self.actor,'cpu',expected_revision=second['revision'],expected_generation=None)
        self.assertEqual(self.app.availability(self.actor,'cpu')['generation'],archived['generation'])

    def test_competing_archive_and_edit_cannot_both_commit(self):
        def archive():
            try:return self.app.archive(self.actor,'cpu',**self.args)
            except Conflict:return None
        def edit():
            try:return self.save(self.record['revision'])
            except Conflict:return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(archive),pool.submit(edit)]
            results=[future.result() for future in futures]
        self.assertEqual(sum(result is not None for result in results),1)

    def test_report_completing_after_delete_cannot_recreate_rule_data(self):
        report=self._tested()
        self.app.delete(self.actor,'cpu',**self.args)
        with self.assertRaises(Conflict):self.store.save_test('cpu',report)
        self.assertEqual(len(self.fixture.client.keys(self.repository._key('cpu','*'))),2)
