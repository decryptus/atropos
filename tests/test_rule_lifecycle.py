import copy
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from atropos.authorization import AccessPolicy, Grant, OPERATIONS, Resource
from atropos.redis_store import Conflict
from atropos.rule_catalog import instantiate
from atropos.rule_lifecycle import Principal, RuleLifecycle
from atropos.rule_lifecycle_store import RedisLifecycleStore
from atropos.rule_repository import RedisRuleRepository
from atropos.rule_model import dump_rules
from support import RedisFixture


class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = RedisFixture()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.close()

    def setUp(self):
        self.fixture.client.flushdb()
        self.actor = Principal('alice', 'org')
        self.inventory = [Resource('org', 'server', 'host', groups={'prod'}),
                          Resource('org', 'server', 'other'),
                          Resource('org', 'container', 'web', 'host', {'webapps'}),
                          Resource('org', 'container', 'api', 'other', {'webapps'})]
        self.grants = [self.grant('alice', OPERATIONS, [('server', 'host'), ('server', 'other')])]
        self.repository = RedisRuleRepository(self.fixture.client, 'org')
        self.store = RedisLifecycleStore(self.repository)
        self.service = RuleLifecycle(self.repository, self.store, lambda: AccessPolicy(self.grants, self.inventory))
        self.document = instantiate(['cpu-high'], name='cpu')
        self.cases = [{'name': 'high', 'input': {'cpu_percent': 95},
                       'expected': {'decision': 'review', 'matched_rule': 'combined'}},
                      {'name': 'normal', 'input': {'cpu_percent': 20},
                       'expected': {'decision': 'ignore', 'matched_rule': '__default__'}}]

    def grant(self, subject, operations, scopes, providers=('internal', 'upload'), kind='intervention'):
        return Grant(subject, 'org', kind, frozenset(operations), frozenset(scopes), providers=frozenset(providers))

    def draft(self, document=None, expected=None, selections=(('container', 'web'),)):
        return self.service.save_draft(self.actor, document or self.document, rule_kind='intervention',
                                        selections=selections, expected_revision=expected)

    def _tested(self, record=None):
        record = record or self.draft()
        report = self.service.test(self.actor, 'cpu', record['revision'], self.cases)
        self.assertTrue(report['passed'])
        return record, report

    def activate(self, record, report, generation=None):
        return self.service.activate(self.actor, 'cpu', record['revision'], test_id=report['id'],
                                     expected_generation=generation)

    def test_draft_edit_keeps_exact_active_version(self):
        first, evidence = self._tested()
        active = self.activate(first, evidence)
        edited = copy.deepcopy(self.document)
        edited['checks']['cpu-high']['gte'] = 99
        second = self.draft(edited, first['revision'])
        self.assertEqual(self.service.active(self.actor, 'cpu')['revision'], first['revision'])
        result = self.service.evaluate_active(self.actor, 'cpu', ('container', 'web'), {'cpu_percent': 95})
        self.assertEqual(result['result']['decision'], 'review')
        self.assertEqual(result['generation'], active['generation'])
        self.assertEqual(self.repository.head('cpu'), second['revision'])
        self.assertFalse(self.fixture.client.exists('atropos:queue'))

    def test_activation_requires_latest_passing_test_for_exact_revision(self):
        first = self.draft()
        with self.assertRaises((ValueError, KeyError)):
            self.service.activate(self.actor, 'cpu', first['revision'], test_id='0'*32, expected_generation=None)
        _, passed = self._tested(first)
        bad = copy.deepcopy(self.cases)
        bad[0]['expected']['decision'] = 'wrong'
        failed = self.service.test(self.actor, 'cpu', first['revision'], bad)
        self.assertFalse(failed['passed'])
        for report in (passed, failed):
            with self.assertRaises(ValueError):
                self.activate(first, report)
        _, new_pass = self._tested(first)
        second = self.draft(expected=first['revision'])
        with self.assertRaises(ValueError):
            self.activate(second, new_pass)
        self.activate(first, new_pass)

    def test_deactivate_reactivate_and_aba_conflict(self):
        first, report = self._tested()
        active = self.activate(first, report)
        stopped = self.service.deactivate(self.actor, 'cpu', expected_generation=active['generation'])
        self.assertIsNone(stopped['revision'])
        with self.assertRaises(Conflict):
            self.service.evaluate_active(self.actor, 'cpu', ('container', 'web'), {'cpu_percent': 95})
        with self.assertRaises(Conflict):
            self.activate(first, report)  # None cannot bypass the intervening deactivate
        new_active = self.activate(first, report, stopped['generation'])
        with self.assertRaises(Conflict):
            self.service.deactivate(self.actor, 'cpu', expected_generation=active['generation'])
        self.assertNotEqual(new_active['generation'], active['generation'])
        events = self.service.audit(self.actor, 'cpu')
        self.assertEqual([e['operation'] for e in events], ['activate', 'deactivate', 'activate'])
        self.assertEqual(events[1]['previous_generation'], events[0]['generation'])

    def test_rollback_only_to_previously_active_version_and_is_audited(self):
        first, one = self._tested()
        active1 = self.activate(first, one)
        second, two = self._tested(self.draft(expected=first['revision']))
        with self.assertRaises(ValueError):
            self.service.rollback(self.actor, 'cpu', second['revision'], test_id=two['id'], expected_generation=active1['generation'])
        active2 = self.activate(second, two, active1['generation'])
        back = self.service.rollback(self.actor, 'cpu', first['revision'], test_id=one['id'], expected_generation=active2['generation'])
        self.assertEqual(back['revision'], first['revision'])
        self.assertEqual(self.repository.head('cpu'), second['revision'])
        self.assertEqual(self.service.audit(self.actor, 'cpu')[-1]['operation'], 'rollback')

    def test_group_selection_is_frozen_and_does_not_select_hosts(self):
        first = self.draft(selections=[('container_group', 'webapps')])
        self.assertEqual(first['context']['targets'], [['container', 'api'], ['container', 'web']])
        self.inventory.append(Resource('org', 'container', 'new', 'host', {'webapps'}))
        record = self.service.read(self.actor, 'cpu', first['revision'])
        self.assertEqual(len(record['context']['targets']), 2)
        record['context']['targets'].append(['server', 'host'])
        self.assertEqual(len(self.service.read(self.actor, 'cpu', first['revision'])['context']['targets']), 2)

    def test_scope_change_requires_rights_on_old_and_new_scopes(self):
        first = self.draft()
        self.grants = [self.grant('alice', OPERATIONS, [('container', 'api')])]
        with self.assertRaises(PermissionError):
            self.draft(expected=first['revision'], selections=[('container', 'api')])
        self.grants = [self.grant('alice', OPERATIONS, [('container', 'web')])]
        with self.assertRaises(PermissionError):
            self.draft(expected=first['revision'], selections=[('container', 'api')])
        self.assertEqual(self.repository.head('cpu'), first['revision'])

    def test_edit_does_not_grant_activation_and_revocation_is_rechecked(self):
        first, report = self._tested()
        self.grants = [self.grant('alice', {'read', 'update', 'save', 'test'}, [('server', 'host')])]
        with self.assertRaises(PermissionError):
            self.activate(first, report)
        self.grants = [self.grant('alice', OPERATIONS, [('server', 'host')])]
        active = self.activate(first, report)
        self.grants = []
        for operation in (lambda: self.service.read(self.actor, 'cpu', first['revision']),
                          lambda: self.service.evaluate_active(self.actor, 'cpu', ('container', 'web'), {'cpu_percent': 95}),
                          lambda: self.service.deactivate(self.actor, 'cpu', expected_generation=active['generation'])):
            with self.assertRaises(PermissionError):
                operation()
        self.assertEqual(self.service.history(self.actor, 'cpu'), [])
        self.assertEqual(self.service.audit(self.actor, 'cpu'), [])

    def test_cross_organization_and_wrong_domain_rejected(self):
        first = self.draft()
        with self.assertRaises(PermissionError):
            self.service.read(Principal('alice', 'other'), 'cpu', first['revision'])
        self.grants = [self.grant('alice', OPERATIONS, [('server', 'host')], kind='supervision')]
        with self.assertRaises(PermissionError):
            self.service.test(self.actor, 'cpu', first['revision'], self.cases)

    def test_import_requires_upload_and_internal_save_permissions(self):
        self.grants = [self.grant('alice', OPERATIONS, [('server', 'host')], providers=('internal',))]
        with self.assertRaises(PermissionError):
            self.service.import_draft(self.actor, dump_rules(self.document), rule_kind='intervention',
                                      selections=[('container', 'web')], expected_revision=None)
        self.grants = [self.grant('alice', OPERATIONS, [('server', 'host')], providers=('upload',))]
        with self.assertRaises(PermissionError):
            self.service.import_draft(self.actor, dump_rules(self.document), rule_kind='intervention',
                                      selections=[('container', 'web')], expected_revision=None)
        self.grants = [self.grant('alice', OPERATIONS, [('server', 'host')])]
        record = self.service.import_draft(self.actor, dump_rules(self.document), rule_kind='intervention',
                                          selections=[('container', 'web')], expected_revision=None)
        self.assertIsNone(self.service.active(self.actor, 'cpu')['revision'])
        self.assertEqual(record['author'], 'alice')

    def test_concurrent_activation_has_one_winner(self):
        first, report = self._tested()
        def run():
            try:
                self.activate(first, report)
                return True
            except Conflict:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: run(), range(2)))
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(len(self.service.audit(self.actor, 'cpu')), 1)

    def test_test_change_between_check_and_commit_rejects_activation(self):
        first, report = self._tested()
        original = self.store.transition
        def interleave(*args, **kwargs):
            bad = copy.deepcopy(self.cases)
            bad[0]['expected']['decision'] = 'wrong'
            self.service.test(self.actor, 'cpu', first['revision'], bad)
            return original(*args, **kwargs)
        with patch.object(self.store, 'transition', interleave), self.assertRaises(Conflict):
            self.activate(first, report)
        self.assertIsNone(self.store.state('cpu')['revision'])

    def test_active_change_during_evaluation_discards_result(self):
        first, report = self._tested()
        active = self.activate(first, report)
        from atropos.zen_rules import ZenRules
        original = ZenRules.evaluate
        def interleave(engine, facts):
            result = original(engine, facts)
            self.service.deactivate(self.actor, 'cpu', expected_generation=active['generation'])
            return result
        with patch('atropos.rule_lifecycle.ZenRules.evaluate', interleave), self.assertRaises(Conflict):
            self.service.evaluate_active(self.actor, 'cpu', ('container', 'web'), {'cpu_percent': 95})

    def test_out_of_scope_evaluation_and_unknown_observations_rejected(self):
        first, report = self._tested()
        self.activate(first, report)
        with self.assertRaises(PermissionError):
            self.service.evaluate_active(self.actor, 'cpu', ('container', 'api'), {'cpu_percent': 95})
        with self.assertRaises(ValueError):
            self.service.evaluate_active(self.actor, 'cpu', ('container', 'web'), {})

    def test_tampered_context_and_old_compiler_evidence_rejected(self):
        first, report = self._tested()
        key = self.store._key('cpu', 'test:' + report['id'])
        report['compiler'] = 'older'
        self.fixture.client.set(key, json.dumps(report))
        with self.assertRaises(ValueError):
            self.activate(first, report)
        raw = copy.deepcopy(first)
        raw['context']['targets'] = [['server', 'other']]
        self.fixture.client.set(self.repository._key('cpu', first['revision']), json.dumps(raw))
        with self.assertRaises(ValueError):
            self.service.read(self.actor, 'cpu', first['revision'])

    def test_lifecycle_evidence_survives_redis_restart(self):
        first, report = self._tested()
        active = self.activate(first, report)
        self.fixture.restart()
        self.assertEqual(self.service.active(self.actor, 'cpu'), active)
        self.assertEqual(self.service.test_report(self.actor, 'cpu', first['revision'], report['id']), report)
        self.assertEqual(len(self.service.audit(self.actor, 'cpu')), 1)

    def test_missing_empty_and_excessive_scopes_rejected(self):
        for selections in ([], [('container_group', 'missing')], [('container', 'missing')], [('container', 'web')]*129):
            with self.subTest(selections=selections[:1]), self.assertRaises(ValueError):
                self.draft(selections=selections)

    def test_identity_with_colon_is_preserved_in_revision_and_audit(self):
        self.actor = Principal('local:alice', 'org')
        self.grants = [self.grant('local:alice', OPERATIONS, [('server', 'host')])]
        first, report = self._tested()
        self.activate(first, report)
        self.assertEqual(first['author'], 'local:alice')
        self.assertEqual(self.service.audit(self.actor, 'cpu')[0]['author'], 'local:alice')

    def test_plain_repository_restore_retains_scope_but_does_not_activate(self):
        first, report = self._tested()
        active = self.activate(first, report)
        restored = self.repository.restore('cpu', first['revision'], 'alice', first['revision'])
        self.assertEqual(restored['context'], first['context'])
        self.assertEqual(self.service.active(self.actor, 'cpu'), active)
        with self.assertRaises(ValueError):
            self.activate(restored, report, active['generation'])
