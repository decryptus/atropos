"""Offline rule import, canonical export, compilation and test commands."""
import json
from pathlib import Path

from .rule_model import MAX_RULE_BYTES, RuleError, dump_rules, load_rules, simple_editor_supported
from .zen_rules import ZenRules, compile_jdm
from .rule_catalog import instantiate, list_templates

MAX_CASES = 100


def add_rule_parser(commands):
    rules = commands.add_parser('rules', help='offline experimental YAML/JSON rules and ZEN tests')
    subcommands = rules.add_subparsers(dest='rule_command', required=True)
    subcommands.add_parser('catalog', help='list built-in draft templates and default parameters')
    init = subcommands.add_parser('init', help='print a self-contained YAML draft; no activation')
    init.add_argument('templates', nargs='+')
    init.add_argument('--name', default='starter-policy')
    init.add_argument('--match', choices=('all', 'any'), default='any')
    init.add_argument('--decision', default='review')
    init.add_argument('--catalog-version', type=int, default=1)
    init.add_argument('--parameters', help='JSON file: template ID to parameter mapping')
    for command in ('check', 'normalize', 'compile', 'evaluate', 'test'):
        item = subcommands.add_parser(command)
        item.add_argument('file', help='rule model in .yaml, .yml or .json; no automatic activation')
        if command == 'evaluate':
            item.add_argument('--input', required=True, help='JSON file containing the declared facts')
        if command == 'test':
            item.add_argument('--cases', required=True, help='JSON scenario file')


def _read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise RuleError('Duplicate JSON key')
            result[key] = value
        return result
    try:
        with Path(path).open('rb') as stream:
            content = stream.read(MAX_RULE_BYTES + 1)
        if len(content) > MAX_RULE_BYTES:
            raise RuleError('JSON input file exceeds 64 KiB')
        return json.loads(content, object_pairs_hook=pairs)
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise RuleError('Invalid JSON input file') from exc


def execute_rule_command(args):
    try:
        if args.rule_command == 'catalog':
            print(json.dumps(list_templates(), indent=2))
            return 0
        if args.rule_command == 'init':
            print(dump_rules(instantiate(
                args.templates, name=args.name, match=args.match, decision=args.decision,
                parameters=_read_json(args.parameters) if args.parameters else None,
                catalog_version=args.catalog_version)), end='')
            return 0
        document = load_rules(args.file)
        if args.rule_command == 'check':
            print(json.dumps({'valid': True, 'name': document['name'],
                              'rules': len(document['rules']),
                              'editor': 'simple' if simple_editor_supported(document) else 'yaml',
                              'activated': False}, indent=2))
        elif args.rule_command == 'normalize':
            print(dump_rules(document), end='')
        elif args.rule_command == 'compile':
            print(json.dumps(compile_jdm(document), ensure_ascii=False, indent=2))
        elif args.rule_command == 'evaluate':
            print(json.dumps(ZenRules(document).evaluate(_read_json(args.input)), indent=2))
        else:
            cases = _read_json(args.cases)
            if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
                raise RuleError('Expected 1–100 scenarios')
            engine = ZenRules(document)
            results = []
            for case in cases:
                if (not isinstance(case, dict) or set(case) != {'name', 'input', 'expected'}
                        or not isinstance(case['name'], str) or len(case['name']) > 128
                        or not isinstance(case['expected'], dict)
                        or set(case['expected']) != {'decision', 'matched_rule'}):
                    raise RuleError('Each scenario requires name, input and a complete expected result')
                try:
                    actual = engine.evaluate(case['input'])
                    results.append({'name': case['name'], 'passed': actual == case['expected'], 'actual': actual})
                except RuleError:
                    results.append({'name': case['name'], 'passed': False, 'error': 'invalid_input_or_evaluation'})
            print(json.dumps({'passed': sum(r['passed'] for r in results), 'total': len(results),
                              'results': results}, indent=2))
            return 0 if all(result['passed'] for result in results) else 1
        return 0
    except (RuleError, OSError) as exc:
        # RuleError contains safe model diagnostics; filesystem errors can expose paths.
        print(json.dumps({'error': str(exc) if isinstance(exc, RuleError) else 'Cannot read rule/input file'}))
        return 2
