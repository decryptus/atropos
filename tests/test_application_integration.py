"""Run explicitly with matching private Atropos sources and its zen dependency.

ATROPOS_SOURCE_ROOT identifies the private checkout for its disposable Redis
fixture. There is no silent skip when an integration dependency is absent.
"""
import importlib.util
import os
from pathlib import Path
import unittest

from atropos.authorization import AccessPolicy, Grant, OPERATIONS, Resource
from atropos.rule_application import RuleActor, build_rule_application

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('atropos_synthetic_fixture', root / 'tests' / 'support.py')
fixture_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture_module)


class AtroposIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = fixture_module.RedisFixture()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.close()

    def setUp(self):
        self.fixture.client.flushdb()
        self.actor = RuleActor('alice', 'org')
        self.resources = [Resource('org', 'server', 'host'),
                          Resource('org', 'container', 'web', 'host', {'apps'})]
        self.grants = [Grant('alice', 'org', 'intervention', frozenset(OPERATIONS),
                             frozenset({('container_group', 'apps')}), providers=frozenset({'internal', 'upload'}))]
        self.application = build_rule_application(
            redis_client=self.fixture.client, actor_loader=lambda: self.actor,
            policy_loader=lambda: AccessPolicy(self.grants, self.resources))
        self.cases = [{'name': 'pressure', 'input': {'cpu_percent': 95},
                       'expected': {'decision': 'review', 'matched_rule': 'combined'}}]

    def draft(self):
        return self.application.create_from_templates(['cpu-high'], name='cpu', rule_kind='intervention',
                                                       selections=[('container_group', 'apps')])

    def test_catalog_to_test_activation_edit_rollback_deactivation(self):
        self.assertEqual(len(self.application.catalog()['templates']), 8)
        first = self.draft()
        evidence = self.application.test('cpu', first['revision'], self.cases)
        active = self.application.activate('cpu', first['revision'], test_id=evidence['id'], expected_generation=None)
        self.assertEqual(self.application.evaluate_active('cpu', ('container', 'web'), {'cpu_percent': 95})['result']['decision'], 'review')
        modified = first['document']
        modified['checks']['cpu-high']['gte'] = 99
        second = self.application.save_draft(modified, rule_kind='intervention', selections=[('container', 'web')],
                                             expected_revision=first['revision'])
        self.assertEqual(self.application.active('cpu')['revision'], first['revision'])
        cases2 = [{'name': 'below new threshold', 'input': {'cpu_percent': 95},
                   'expected': {'decision': 'ignore', 'matched_rule': '__default__'}}]
        report2 = self.application.test('cpu', second['revision'], cases2)
        active2 = self.application.activate('cpu', second['revision'], test_id=report2['id'], expected_generation=active['generation'])
        self.assertEqual(self.application.evaluate_active('cpu', ('container', 'web'), {'cpu_percent': 95})['result']['decision'], 'ignore')
        back = self.application.rollback('cpu', first['revision'], test_id=evidence['id'], expected_generation=active2['generation'])
        self.application.deactivate('cpu', expected_generation=back['generation'])
        self.assertIsNone(self.application.active('cpu')['revision'])
        self.assertEqual(len(self.application.history('cpu')), 2)
        self.assertEqual([e['operation'] for e in self.application.audit('cpu')], ['activate', 'activate', 'rollback', 'deactivate'])
        self.assertEqual(self.application.test_report('cpu', first['revision'], evidence['id'])['author'], 'alice')
        self.assertFalse(self.fixture.client.exists('atropos:queue'))

    def test_identity_org_and_policy_changes_take_effect(self):
        first = self.draft()
        self.actor = RuleActor('bob', 'org')
        with self.assertRaises(PermissionError):
            self.application.read('cpu', first['revision'])
        self.actor = RuleActor('alice', 'other')
        with self.assertRaises(KeyError):
            self.application.read('cpu', first['revision'])
        self.actor = RuleActor('alice', 'org')
        self.grants = []
        with self.assertRaises(PermissionError):
            self.application.test('cpu', first['revision'], self.cases)

    def test_file_import_stays_inactive(self):
        from atropos.rule_catalog import instantiate
        from atropos.rule_model import dump_rules
        imported = self.application.import_draft(dump_rules(instantiate(['cpu-high'], name='cpu')),
                    rule_kind='intervention', selections=[('container', 'web')], expected_revision=None)
        self.assertEqual(imported['context']['targets'], [['container', 'web']])
        self.assertIsNone(self.application.active('cpu')['revision'])
