import asyncio
import copy
import io
import itertools
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from atropos.actions import builtin_actions
from atropos.cli import main
from atropos.redis_store import RedisStore
from atropos.rule_model import (
    RuleError, dump_rules, load_rules, parse_rules, replace_from_simple_editor,
    simple_editor_supported, validate_rules,
)
from atropos.service import WorkflowService
from atropos.worker import Worker
from atropos.zen_rules import ZenRules, compile_jdm, zen_rule_action
from support import RedisFixture, workflow

EXAMPLES = Path(__file__).resolve().parents[1] / 'examples' / 'rules'


class ZenRuleTests(unittest.TestCase):
    def setUp(self):
        self.memory = load_rules(EXAMPLES / 'memory.yml')
        self.storage = load_rules(EXAMPLES / 'storage.yml')

    def test_memory_grid_uses_real_zen(self):
        engine = ZenRules(self.memory)
        for memory, maintenance, restarts in itertools.product((0, 89.9, 90, 99), (False, True), (0, 1, 2, 4)):
            expected = (('hold', 'maintenance') if maintenance else
                        ('notify', 'repeated_restart') if restarts >= 2 else
                        ('diagnose', 'high_memory') if memory >= 90 else ('ignore', '__default__'))
            with self.subTest(memory=memory, maintenance=maintenance, restarts=restarts):
                self.assertEqual(engine.evaluate({'memory_percent': memory, 'maintenance': maintenance,
                                                  'restart_count': restarts}),
                                 {'decision': expected[0], 'matched_rule': expected[1]})

    def test_nested_all_any_not_with_real_zen(self):
        engine = ZenRules(self.storage)
        for disk, inodes, writable, maintenance in itertools.product((10, 90), (10, 95), (False, True), (False, True)):
            expected = not maintenance and (disk >= 90 or inodes >= 95 or not writable)
            result = engine.evaluate({'disk_percent': disk, 'inode_percent': inodes,
                                      'writable': writable, 'maintenance': maintenance})
            self.assertEqual(result['decision'], 'diagnose' if expected else 'ignore')

    def test_all_comparisons_and_membership(self):
        cases = [('eq', 5, 5, True), ('ne', 5, 4, True), ('gt', 5, 6, True),
                 ('gte', 5, 5, True), ('lt', 5, 4, True), ('lte', 5, 5, True),
                 ('in', [4, 5], 5, True), ('not_in', [4, 5], 6, True),
                 ('not_in', [4, 5], 4, False)]
        for operator, threshold, value, matched in cases:
            model = {'version': 1, 'name': 'comparison', 'inputs': {'value': 'number'},
                     'rules': [{'id': 'test', 'when': {'field': 'value', operator: threshold}, 'then': 'match'}],
                     'default': 'ignore'}
            with self.subTest(operator=operator, value=value):
                self.assertEqual(ZenRules(model).evaluate({'value': value})['decision'],
                                 'match' if matched else 'ignore')

    def test_strings_remain_literals_in_compiled_expressions(self):
        for value in ('échec', 'a" or true or "b', 'line\nbreak', 'back\\slash', '${1+1}', 'on'):
            model = {'version': 1, 'name': 'strings', 'inputs': {'true': 'string'},
                     'rules': [{'id': 'test', 'when': {'field': 'true', 'eq': value}, 'then': 'match'}],
                     'default': 'ignore'}
            with self.subTest(value=value):
                engine = ZenRules(model)
                self.assertEqual(engine.evaluate({'true': value})['decision'], 'match')
                self.assertEqual(engine.evaluate({'true': 'different'})['decision'], 'ignore')

    def test_first_matching_rule_wins(self):
        original = ZenRules(self.memory).evaluate({'memory_percent': 99, 'maintenance': True, 'restart_count': 4})
        self.memory['rules'][0], self.memory['rules'][1] = self.memory['rules'][1], self.memory['rules'][0]
        reordered = ZenRules(self.memory).evaluate({'memory_percent': 99, 'maintenance': True, 'restart_count': 4})
        self.assertEqual(original['decision'], 'hold')
        self.assertEqual(reordered['decision'], 'notify')

    def test_yaml_json_model_yaml_roundtrip(self):
        for document in (self.memory, self.storage):
            imported_json = parse_rules(json.dumps(document), 'json')
            exported_yaml = dump_rules(imported_json)
            imported_yaml = parse_rules(exported_yaml)
            self.assertEqual(document, imported_yaml)
            self.assertEqual(compile_jdm(document), compile_jdm(imported_yaml))

    def test_simple_form_edit_then_yaml_has_same_decision(self):
        form = copy.deepcopy(self.memory)
        form['rules'][2]['when']['all'][0]['gte'] = 95
        updated = replace_from_simple_editor(self.memory, form)
        reloaded = parse_rules(dump_rules(updated))
        facts = {'memory_percent': 94, 'maintenance': False, 'restart_count': 0}
        self.assertEqual(ZenRules(self.memory).evaluate(facts)['decision'], 'diagnose')
        self.assertEqual(ZenRules(reloaded).evaluate(facts)['decision'], 'ignore')

    def test_advanced_rule_cannot_be_silently_flattened_by_form(self):
        self.assertTrue(simple_editor_supported(self.memory))
        self.assertFalse(simple_editor_supported(self.storage))
        original = copy.deepcopy(self.storage)
        with self.assertRaises(RuleError):
            replace_from_simple_editor(self.storage, self.memory)
        self.assertEqual(self.storage, original)

    def test_missing_null_extra_and_wrong_types_are_rejected(self):
        engine = ZenRules(self.memory)
        valid = {'memory_percent': 94, 'maintenance': False, 'restart_count': 0}
        variants = [{}, dict(valid, memory_percent=None), dict(valid, memory_percent='94'),
                    dict(valid, memory_percent=True), dict(valid, restart_count=1.5),
                    dict(valid, maintenance=0), dict(valid, other=1),
                    dict(valid, memory_percent=float('nan')), dict(valid, memory_percent=10 ** 400)]
        for facts in variants:
            with self.subTest(facts=str(facts)[:100]), self.assertRaises(RuleError):
                engine.evaluate(facts)

    def test_unsupported_logic_and_references_rejected(self):
        conditions = [{'expression': 'arbitrary()'}, {'field': 'missing', 'eq': 0},
                      {'field': 'maintenance', 'gt': False}, {'all': []},
                      {'field': 'memory_percent', 'eq': '90'}, {'field': 'memory_percent', 'in': []}]
        for condition in conditions:
            model = copy.deepcopy(self.memory)
            model['rules'][0]['when'] = condition
            with self.subTest(condition=condition), self.assertRaises(RuleError):
                compile_jdm(model)

    def test_rule_ids_and_complexity_are_bounded(self):
        duplicate = copy.deepcopy(self.memory)
        duplicate['rules'][1]['id'] = duplicate['rules'][0]['id']
        with self.assertRaises(RuleError):
            validate_rules(duplicate)
        nested = {'field': 'maintenance', 'eq': False}
        for _ in range(10):
            nested = {'not': nested}
        self.memory['rules'][0]['when'] = nested
        with self.assertRaises(RuleError):
            validate_rules(self.memory)

    def test_yaml_and_json_import_validation(self):
        invalid = [('version: 1\nversion: 1', 'yaml'),
                   ('version: &v 1\nname: *v', 'yaml'),
                   ('{"version":1,"version":1}', 'json'),
                   ('{', 'json'), ('x' * 65537, 'yaml')]
        for text, format in invalid:
            with self.subTest(format=format), self.assertRaises(RuleError):
                parse_rules(text, format)

    def test_engine_keeps_a_snapshot_of_approved_rules(self):
        engine = ZenRules(self.memory)
        self.memory['rules'][2]['then'] = 'changed'
        result = engine.evaluate({'memory_percent': 94, 'maintenance': False, 'restart_count': 0})
        self.assertEqual(result['decision'], 'diagnose')

    def test_compilation_is_deterministic_and_tracks_source(self):
        first = compile_jdm(self.memory)
        self.assertEqual(first, compile_jdm(parse_rules(dump_rules(self.memory))))
        self.memory['rules'][2]['when']['all'][0]['gte'] = 95
        self.assertNotEqual(first['atropos']['source_sha256'], compile_jdm(self.memory)['atropos']['source_sha256'])

    def test_cli_import_normalize_compile_and_scenario_suite(self):
        with tempfile.TemporaryDirectory() as root:
            imported = Path(root) / 'uploaded.json'
            imported.write_text(json.dumps(self.memory))
            for args in (
                ['rules', 'check', str(imported)],
                ['rules', 'normalize', str(imported)],
                ['rules', 'compile', str(imported)],
                ['rules', 'evaluate', str(imported), '--input', str(EXAMPLES / 'memory-input.json')],
                ['rules', 'test', str(imported), '--cases', str(EXAMPLES / 'memory-cases.json')],
            ):
                with self.subTest(command=args[1]), redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(main(args), 0)
                self.assertTrue(out.getvalue())

    def test_cli_failed_case_returns_failure(self):
        with tempfile.TemporaryDirectory() as root:
            cases = Path(root) / 'cases.json'
            cases.write_text(json.dumps([{'name': 'intentional mismatch',
                                         'input': {'memory_percent': 94, 'maintenance': False, 'restart_count': 0},
                                         'expected': {'decision': 'wrong', 'matched_rule': 'high_memory'}}]))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(['rules', 'test', str(EXAMPLES / 'memory.yml'), '--cases', str(cases)]), 1)

    def test_zen_decision_drives_persisted_workflow(self):
        fixture = RedisFixture()
        try:
            store = RedisStore(fixture.client)
            store.initialize()
            actions = builtin_actions()
            actions['memory.decide'] = zen_rule_action(self.memory)
            definition = workflow({
                'first': {'action': 'memory.decide', 'with': {
                    'memory_percent': {'$ref': '/input/memory_percent'},
                    'maintenance': {'$ref': '/input/maintenance'},
                    'restart_count': {'$ref': '/input/restart_count'}},
                    'on_decision': {'diagnose': 'report', 'hold': None, 'notify': None, 'ignore': None}},
                'report': {'action': 'core.echo', 'with': {'matched_rule': {'$ref': '/results/first/matched_rule'}}},
            })
            run = WorkflowService(store, actions).submit(definition,
                {'memory_percent': 94, 'maintenance': False, 'restart_count': 0}, 'synthetic')
            async def execute():
                worker = Worker(store, actions)
                await worker.run_once(0)
                await worker.run_once(0)
            asyncio.run(execute())
            state = store.get(run['id'])
            self.assertEqual(state['status'], 'succeeded')
            self.assertEqual(state['results']['report'], {'matched_rule': 'high_memory'})
        finally:
            fixture.close()
