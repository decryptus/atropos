import copy
import tempfile
import unittest
from pathlib import Path

from atropos.actions import Action, builtin_actions, echo
from atropos.definitions import DefinitionError, encode, load, resolve, validate
from atropos.service import WorkflowService
from support import workflow


class DefinitionTests(unittest.TestCase):
    def test_valid_definition_is_copied(self):
        source = workflow()
        validated = validate(source, builtin_actions())
        validated['name'] = 'other'
        self.assertEqual(source['name'], 'test')

    def test_duplicate_yaml_and_aliases_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'rule.yml'
            for content in ('name: first\nname: second', 'name: &n test\nstart: *n'):
                with self.subTest(content=content):
                    path.write_text(content)
                    with self.assertRaises(DefinitionError):
                        load(path)

    def test_invalid_graphs_rejected(self):
        for steps in (
            {'first': {'action': 'core.echo', 'next': 'missing'}},
            {'first': {'action': 'core.echo', 'next': 'first'}},
            {'first': {'action': 'core.echo'}, 'unused': {'action': 'core.echo'}},
        ):
            with self.subTest(steps=steps), self.assertRaises(DefinitionError):
                validate(workflow(steps), builtin_actions())

    def test_unregistered_action_rejected(self):
        with self.assertRaises(DefinitionError):
            validate(workflow({'first': {'action': 'shell.run'}}), builtin_actions())

    def test_unsafe_action_cannot_retry(self):
        actions = {'unsafe': Action(echo)}
        with self.assertRaises(DefinitionError):
            validate(workflow({'first': {'action': 'unsafe', 'retry': {'attempts': 2}}}), actions)

    def test_strict_types_and_limits(self):
        for patch in ({'timeout': True}, {'timeout': float('inf')},
                      {'retry': {'attempts': True}}, {'retry': {'attempts': 6}},
                      {'with': []}, {'unknown': 1}):
            document = workflow()
            document['steps']['first'].update(patch)
            with self.subTest(patch=patch), self.assertRaises(DefinitionError):
                validate(document, builtin_actions())

    def test_explicit_references(self):
        context = {'input': {'a/b': [2, 4]}}
        self.assertEqual(resolve({'x': {'$ref': '/input/a~1b/1'}}, context), {'x': 4})
        for pointer in ('/input/a~1b/-1', '/input/a~1b/01', '/input/a~2b', '/missing'):
            with self.subTest(pointer=pointer), self.assertRaises(DefinitionError):
                resolve({'$ref': pointer}, context)

    def test_non_json_and_size_rejected(self):
        for value in ({'x': float('nan')}, {'x': object()}):
            with self.assertRaises(DefinitionError):
                encode(value)
        with self.assertRaises(DefinitionError):
            encode({'x': 'a' * 100}, 10)

    def test_plan_has_no_storage_or_action_effects(self):
        async def forbidden(params, context):
            self.fail('plan executed an action')
        document = workflow({'first': {'action': 'test.action'}})
        plan = WorkflowService(None, {'test.action': Action(forbidden)}).plan(document)
        self.assertEqual(plan['steps'], ['first'])
        self.assertIn('structural plan only', plan['effects'])
