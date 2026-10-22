import importlib
import unittest
from unittest.mock import Mock

from atropos.rule_application import RuleActor, RuleApplication


class RuleApplicationTests(unittest.TestCase):
    def setUp(self):
        self.actor = RuleActor('alice', 'org')
        self.backend = Mock()
        self.factory = Mock(return_value=(self.backend, self.actor))
        self.loader = Mock(return_value=self.actor)
        self.templates = Mock(return_value={'templates': []})
        self.instantiate = Mock(return_value={'name': 'rule'})
        self.application = RuleApplication(self.loader, self.factory, self.templates, self.instantiate)

    def test_actor_is_resolved_for_each_request(self):
        self.application.read('rule', 'revision')
        self.backend.read.assert_called_once_with(self.actor, 'rule', 'revision')
        second = RuleActor('bob', 'other')
        self.loader.return_value = second
        self.factory.return_value = (self.backend, second)
        self.application.active('rule')
        self.backend.active.assert_called_once_with(second, 'rule')
        self.assertEqual(self.loader.call_count, 2)

    def test_missing_authenticated_actor_fails_before_backend(self):
        self.loader.return_value = None
        with self.assertRaises(PermissionError):
            self.application.catalog()
        self.factory.assert_not_called()

    def test_catalog_creation_is_only_a_draft(self):
        self.application.create_from_templates(['cpu-high'], name='rule', rule_kind='intervention',
                                                selections=[('container_group', 'web')], match='all',
                                                parameters={'cpu-high': {'threshold': 85}})
        self.backend.save_draft.assert_called_once()
        kwargs = self.backend.save_draft.call_args.kwargs
        self.assertIsNone(kwargs['expected_revision'])
        self.assertEqual(kwargs['selections'], [('container_group', 'web')])
        self.backend.activate.assert_not_called()
        self.backend.evaluate_active.assert_not_called()

    def test_request_cannot_override_actor(self):
        with self.assertRaises(TypeError):
            self.application.read('rule', 'revision', actor=RuleActor('admin', 'org'))

    def test_activation_preserves_explicit_version_test_and_generation(self):
        self.application.activate('rule', 'revision', test_id='report', expected_generation='previous')
        self.backend.activate.assert_called_once_with(self.actor, 'rule', 'revision', test_id='report',
                                                      expected_generation='previous')
        self.application.deactivate('rule', expected_generation='generation')
        self.backend.deactivate.assert_called_once_with(self.actor, 'rule', expected_generation='generation')

