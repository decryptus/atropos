import copy
import io
import itertools
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from atropos.cli import main
from atropos.rule_catalog import instantiate, list_templates
from atropos.rule_model import RuleError, dump_rules, parse_rules, simple_editor_supported, validate_rules
from atropos.rule_repository import RedisRuleRepository
from atropos.zen_rules import ZenRules, compile_jdm
from support import RedisFixture


class CatalogTests(unittest.TestCase):
    def test_every_template_boundary_with_real_zen(self):
        observations = {
            'cpu-high': ('cpu_percent', 89, 90),
            'memory-high': ('memory_percent', 89, 90),
            'disk-high': ('disk_percent', 89, 90),
            'inodes-high': ('inode_percent', 89, 90),
            'directory-unreadable': ('readable', True, False),
            'directory-unwritable': ('writable', True, False),
            'container-unhealthy': ('health_status', 'healthy', 'unhealthy'),
            'restarts-high': ('restart_count', 2, 3),
        }
        self.assertEqual({item['id'] for item in list_templates()['templates']}, set(observations))
        for key, (field, normal, triggered) in observations.items():
            with self.subTest(template=key):
                engine = ZenRules(instantiate([key]))
                self.assertEqual(engine.evaluate({field: normal})['decision'], 'ignore')
                self.assertEqual(engine.evaluate({field: triggered})['decision'], 'review')

    def test_all_and_any_truth_tables(self):
        for mode in ('all', 'any'):
            engine = ZenRules(instantiate(['cpu-high', 'memory-high'], match=mode))
            for cpu, memory in itertools.product((10, 95), repeat=2):
                expected = (all if mode == 'all' else any)((cpu >= 90, memory >= 90))
                with self.subTest(mode=mode, cpu=cpu, memory=memory):
                    self.assertEqual(engine.evaluate({'cpu_percent': cpu, 'memory_percent': memory}),
                                     {'decision': 'review' if expected else 'ignore',
                                      'matched_rule': 'combined' if expected else '__default__'})

    def test_nested_references_not_and_first_match(self):
        model = instantiate(['cpu-high', 'memory-high'])
        model['inputs']['maintenance'] = 'boolean'
        model['checks']['pressure'] = {'any': [{'ref': 'cpu-high'}, {'ref': 'memory-high'}]}
        model['rules'][0]['when'] = {'all': [{'ref': 'pressure'}, {'not': {'field': 'maintenance', 'eq': True}}]}
        model['rules'].insert(0, {'id': 'hold', 'when': {'field': 'maintenance', 'eq': True}, 'then': 'hold'})
        engine = ZenRules(model)
        self.assertEqual(engine.evaluate({'cpu_percent': 95, 'memory_percent': 10, 'maintenance': True})['decision'], 'hold')
        self.assertEqual(engine.evaluate({'cpu_percent': 95, 'memory_percent': 10, 'maintenance': False})['decision'], 'review')

    def test_parameter_overrides_are_snapshots(self):
        options = {'cpu-high': {'threshold': 80}}
        model = instantiate(['cpu-high'], parameters=options)
        options['cpu-high']['threshold'] = 99
        model2 = instantiate(['cpu-high'])
        self.assertEqual(ZenRules(model).evaluate({'cpu_percent': 85})['decision'], 'review')
        self.assertEqual(ZenRules(model2).evaluate({'cpu_percent': 85})['decision'], 'ignore')
        self.assertNotEqual(compile_jdm(model)['atropos']['source_sha256'], compile_jdm(model2)['atropos']['source_sha256'])

    def test_invalid_templates_versions_parameters(self):
        variants = [([], {}), (['missing'], {}), (['cpu-high', 'cpu-high'], {}),
                    (['cpu-high'], {'catalog_version': 2}), (['cpu-high'], {'match': 'xor'}),
                    (['cpu-high'], {'parameters': {'memory-high': {}}}),
                    (['cpu-high'], {'parameters': {'cpu-high': {'duration': 60}}}),
                    (['directory-unreadable'], {'parameters': {'directory-unreadable': {'threshold': 1}}})]
        variants += [(['cpu-high'], {'parameters': {'cpu-high': {'threshold': value}}})
                     for value in (-1, 101, True, '90', float('nan'), float('inf'))]
        variants += [(['restarts-high'], {'parameters': {'restarts-high': {'threshold': value}}})
                     for value in (0, -1, 1.5, True)]
        for selected, kwargs in variants:
            with self.subTest(selected=selected, kwargs=kwargs), self.assertRaises(RuleError):
                instantiate(selected, **kwargs)

    def test_missing_cyclic_and_unused_invalid_checks_rejected(self):
        original = instantiate(['cpu-high'])
        variants = [{'a': {'ref': 'a'}}, {'a': {'ref': 'b'}, 'b': {'ref': 'a'}},
                    {'a': {'ref': 'missing'}}, {'a': {'field': 'undeclared', 'eq': 1}}]
        for extra in variants:
            model = copy.deepcopy(original)
            model['checks'].update(extra)
            with self.subTest(extra=extra), self.assertRaises(RuleError):
                validate_rules(model)
        original['rules'][0]['when'] = {'ref': 'combined'}
        with self.assertRaises(RuleError):
            validate_rules(original)  # refs mean checks, never a previous selected decision

    def test_reference_expansion_is_bounded(self):
        model = instantiate(['cpu-high'])
        previous = 'cpu-high'
        for index in range(7):
            key = f'group{index}'
            model['checks'][key] = {'any': [{'ref': previous}, {'ref': previous}]}
            previous = key
        model['rules'][0]['when'] = {'ref': previous}
        with self.assertRaises(RuleError):
            validate_rules(model)

    def test_roundtrip_preserves_refs_and_rejects_lossy_form_edit(self):
        model = instantiate(['cpu-high', 'memory-high'], match='all')
        restored = parse_rules(dump_rules(parse_rules(json.dumps(model), 'json')))
        self.assertEqual(restored, model)
        self.assertEqual(compile_jdm(restored), compile_jdm(model))
        self.assertFalse(simple_editor_supported(model))

    def test_missing_observation_is_not_healthy(self):
        engine = ZenRules(instantiate(['cpu-high', 'memory-high']))
        for facts in ({'cpu_percent': 99}, {'cpu_percent': 99, 'memory_percent': None}):
            with self.assertRaises(RuleError):
                engine.evaluate(facts)

    def test_cli_catalog_init_override_and_compile(self):
        with tempfile.TemporaryDirectory() as root:
            params = Path(root) / 'parameters.json'
            params.write_text('{"cpu-high": {"threshold": 80}}')
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(['rules', 'catalog']), 0)
            self.assertEqual(json.loads(output.getvalue())['catalog_version'], 1)
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(['rules', 'init', 'cpu-high', 'memory-high', '--match', 'all',
                                       '--parameters', str(params)]), 0)
            model = parse_rules(output.getvalue())
            self.assertEqual(model['checks']['cpu-high']['gte'], 80)
            self.assertEqual(ZenRules(model).evaluate({'cpu_percent': 85, 'memory_percent': 95})['decision'], 'review')
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(['rules', 'init', 'unknown']), 2)

    def test_versioned_storage_retains_concrete_defaults_and_restores_draft(self):
        fixture = RedisFixture()
        try:
            repository = RedisRuleRepository(fixture.client, organization='synthetic')
            first = repository.save(instantiate(['cpu-high']), author='alice', expected_revision=None)
            second = repository.save(instantiate(['cpu-high'], parameters={'cpu-high': {'threshold': 80}}),
                                     author='alice', expected_revision=first['revision'])
            restored = repository.restore('starter-policy', first['revision'], author='alice',
                                          expected_revision=second['revision'])
            original = repository.get('starter-policy', first['revision'])
            self.assertEqual(original['document']['checks']['cpu-high']['gte'], 90)
            self.assertEqual(restored['document'], original['document'])
            self.assertNotEqual(restored['revision'], first['revision'])
        finally:
            fixture.close()
