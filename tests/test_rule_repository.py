import copy
import unittest
from pathlib import Path

from atropos.redis_store import Conflict
from atropos.rule_model import load_rules, parse_rules
from atropos.rule_repository import RedisRuleRepository
from support import RedisFixture


class RuleRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = RedisFixture()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.close()

    def setUp(self):
        self.fixture.client.flushdb()
        self.repository = RedisRuleRepository(self.fixture.client, 'org')
        self.document = load_rules(Path(__file__).resolve().parents[1] / 'examples/rules/memory.yml')

    def test_save_versions_export_and_restore_without_overwriting_history(self):
        first = self.repository.save(self.document, 'alice', None, 'Initial version')
        self.document['default'] = 'notify'
        second = self.repository.save(self.document, 'bob', first['revision'], 'Change default')
        restored = self.repository.restore(self.document['name'], first['revision'], 'alice', second['revision'])
        self.assertEqual(restored['parent'], second['revision'])
        self.assertEqual(restored['document']['default'], 'ignore')
        history = self.repository.history(self.document['name'])
        self.assertEqual([v['author'] for v in history], ['alice', 'bob', 'alice'])
        self.assertEqual(self.repository.get(self.document['name'], second['revision'])['document']['default'], 'notify')
        exported = self.repository.export_yaml(self.document['name'], restored['revision'])
        self.assertEqual(parse_rules(exported), restored['document'])

    def test_stale_editor_cannot_overwrite_new_revision(self):
        first = self.repository.save(self.document, 'alice', None)
        with self.assertRaises(Conflict):
            self.repository.save(self.document, 'bob', None)
        self.repository.save(self.document, 'alice', first['revision'])
        with self.assertRaises(Conflict):
            self.repository.save(self.document, 'bob', first['revision'])

    def test_save_does_not_activate_or_enqueue(self):
        self.repository.save(self.document, 'alice', None)
        self.assertFalse(self.fixture.client.exists('atropos:queue'))
        self.assertFalse(self.fixture.client.exists('atropos:rules:org:memory-policy:active'))

    def test_organizations_have_separate_revision_namespaces(self):
        first = self.repository.save(self.document, 'alice', None)
        other = RedisRuleRepository(self.fixture.client, 'other')
        self.assertEqual(other.history('memory-policy'), [])
        with self.assertRaises(KeyError):
            other.get('memory-policy', first['revision'])

    def test_revisions_survive_restart(self):
        first = self.repository.save(self.document, 'alice', None)
        self.fixture.restart()
        self.assertEqual(self.repository.get('memory-policy', first['revision'])['document'], self.document)

    def test_history_is_paginated(self):
        first = self.repository.save(self.document, 'alice', None)
        second = self.repository.save(self.document, 'alice', first['revision'])
        self.assertEqual(self.repository.history('memory-policy', 1, 1)[0]['revision'], second['revision'])
