"""Experimental shared authoring model for forms, YAML and JSON imports.

This module validates and serializes rules. It never evaluates a condition or
activates a rule. Rule list order is significant: first match wins.
"""
import copy
import json
import math
import re
from pathlib import Path

import yaml

from .definitions import DefinitionError, StrictLoader, encode, name

MAX_RULE_BYTES = 65536
MAX_RULES = 32
MAX_INPUTS = 32
MAX_CONDITION_NODES = 128
MAX_CONDITION_DEPTH = 8
MAX_TEXT_LENGTH = 256
MAX_LIST_VALUES = 32
MAX_NUMBER = 2 ** 53 - 1
FIELD_NAME = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,63}')
DOCUMENT_REQUIRED = frozenset({'version', 'name', 'inputs', 'rules', 'default'})
RULE_REQUIRED = frozenset({'id', 'when', 'then'})
INPUT_TYPES = frozenset({'number', 'integer', 'boolean', 'string'})
COMPARISONS = frozenset({'eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'in', 'not_in'})
ORDERED = frozenset({'gt', 'gte', 'lt', 'lte'})


class RuleError(ValueError):
    """A safe structural/type error; no original data or expression included."""


class RuleLoader(StrictLoader):
    # Use only true/false as booleans; strings such as on/off/yes stay strings.
    yaml_implicit_resolvers = {
        key: [(tag, regex) for tag, regex in rules if tag != 'tag:yaml.org,2002:bool']
        for key, rules in StrictLoader.yaml_implicit_resolvers.items()
    }


RuleLoader.add_implicit_resolver(
    'tag:yaml.org,2002:bool', re.compile(r'^(?:true|false)$'), list('tf'))


def _require(condition, message):
    if not condition:
        raise RuleError(message)


def _bounded_json(value):
    try:
        encode(value, MAX_RULE_BYTES)
    except (DefinitionError, UnicodeError) as exc:
        raise RuleError('Rules must be bounded finite JSON-compatible data') from exc


def _identifier(value, location):
    try:
        return name(value, location)
    except DefinitionError as exc:
        raise RuleError(f'{location}: invalid identifier') from exc


def _description(value, location):
    _require(isinstance(value, str) and len(value) <= MAX_TEXT_LENGTH,
             f'{location}: expected a string of at most 256 characters')


def _scalar(value, kind, location):
    valid = False
    if kind == 'boolean':
        valid = type(value) is bool
    elif kind == 'string':
        valid = isinstance(value, str) and len(value) <= MAX_TEXT_LENGTH
    elif kind == 'integer':
        valid = type(value) is int and abs(value) <= MAX_NUMBER
    elif kind == 'number':
        valid = type(value) in (int, float) and abs(value) <= MAX_NUMBER and math.isfinite(value)
    _require(valid, f'{location}: expected a bounded {kind}; no implicit conversion')


def _condition(value, inputs, budget, depth=0, checks=None, trail=()):
    budget[0] += 1
    _require(depth <= MAX_CONDITION_DEPTH and budget[0] <= MAX_CONDITION_NODES,
             'Condition complexity limit exceeded')
    _require(isinstance(value, dict), 'Condition must be an object')
    if set(value) == {'ref'}:
        ref = _identifier(value['ref'], 'condition.ref')
        _require(checks is not None and ref in checks, 'Unknown local check reference')
        _require(ref not in trail, 'Cyclic check reference')
        _condition(checks[ref], inputs, budget, depth + 1, checks, trail + (ref,))
        return
    if set(value) in ({'all'}, {'any'}):
        key = next(iter(value))
        children = value[key]
        _require(isinstance(children, list) and 1 <= len(children) <= MAX_CONDITION_NODES,
                 'all/any requires a nonempty bounded list')
        for child in children:
            _condition(child, inputs, budget, depth + 1, checks, trail)
        return
    if set(value) == {'not'}:
        _condition(value['not'], inputs, budget, depth + 1, checks, trail)
        return
    _require(len(value) == 2 and 'field' in value, 'Expected field and one comparison')
    field = value['field']
    _require(isinstance(field, str) and field in inputs, 'Condition references an undeclared input')
    operator = next(key for key in value if key != 'field')
    _require(operator in COMPARISONS, 'Unsupported comparison')
    kind = inputs[field]
    _require(operator not in ORDERED or kind in ('number', 'integer'),
             'Ordered comparisons require a numeric input')
    values = value[operator]
    if operator in ('in', 'not_in'):
        _require(isinstance(values, list) and 1 <= len(values) <= MAX_LIST_VALUES,
                 'Membership requires a list of 1–32 values')
    else:
        values = [values]
    for item in values:
        _scalar(item, kind, 'Comparison value')


def validate_rules(document):
    _bounded_json(document)
    _require(isinstance(document, dict) and DOCUMENT_REQUIRED <= set(document)
             and set(document) <= DOCUMENT_REQUIRED | {'description', 'checks'},
             'Rules require version, name, inputs, rules and default')
    _require(type(document['version']) is int and document['version'] == 1,
             'Unsupported rule model version')
    _identifier(document['name'], 'name')
    _identifier(document['default'], 'default')
    if 'description' in document:
        _description(document['description'], 'description')
    inputs = document['inputs']
    _require(isinstance(inputs, dict) and 1 <= len(inputs) <= MAX_INPUTS,
             'inputs requires 1–32 named types')
    for field, kind in inputs.items():
        _require(isinstance(field, str) and FIELD_NAME.fullmatch(field) is not None,
                 'Input name must be a simple identifier')
        _require(isinstance(kind, str) and kind in INPUT_TYPES, 'Unsupported input type')
    rules = document['rules']
    _require(isinstance(rules, list) and 1 <= len(rules) <= MAX_RULES,
             'rules requires 1–32 ordered rules')
    checks = document.get('checks', {})
    _require(isinstance(checks, dict) and len(checks) <= MAX_RULES,
             'checks requires at most 32 named conditions')
    for identifier in checks:
        _identifier(identifier, 'check name')
    ids, budget = set(), [0]
    # Validate even unused checks, and bound expanded work, not only source size.
    for identifier, condition in checks.items():
        _condition(condition, inputs, budget, checks=checks, trail=(identifier,))
    for rule in rules:
        _require(isinstance(rule, dict) and RULE_REQUIRED <= set(rule)
                 and set(rule) <= RULE_REQUIRED | {'description'},
                 'Each rule requires id, when and then')
        identifier = _identifier(rule['id'], 'rule.id')
        _require(identifier not in ids, 'Duplicate rule identifier')
        ids.add(identifier)
        _identifier(rule['then'], 'rule.then')
        if 'description' in rule:
            _description(rule['description'], 'rule.description')
        _condition(rule['when'], inputs, budget, checks=checks)
    return copy.deepcopy(document)


def expand_condition(condition, checks):
    """Expand local references only after validate_rules has bounded the graph."""
    if 'ref' in condition:
        return expand_condition(checks[condition['ref']], checks)
    for group in ('all', 'any'):
        if group in condition:
            return {group: [expand_condition(child, checks) for child in condition[group]]}
    if 'not' in condition:
        return {'not': expand_condition(condition['not'], checks)}
    return copy.deepcopy(condition)


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'Duplicate JSON key')
        result[key] = value
    return result


def parse_rules(content, format='yaml'):
    _require(isinstance(content, (str, bytes)), 'Expected YAML or JSON text')
    try:
        _require(len(content.encode('utf-8') if isinstance(content, str) else content) <= MAX_RULE_BYTES,
                 'Rule file exceeds 64 KiB')
        if format == 'yaml':
            document = yaml.load(content, Loader=RuleLoader)
        elif format == 'json':
            document = json.loads(content, object_pairs_hook=_json_pairs)
        else:
            raise RuleError('Supported import formats are yaml and json')
    except (yaml.YAMLError, DefinitionError, UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise RuleError('Invalid rule document') from exc
    return validate_rules(document)


def load_rules(path):
    path = Path(path)
    _require(path.suffix.lower() in ('.yaml', '.yml', '.json'),
             'Rule file must have .yaml, .yml or .json extension')
    with path.open('rb') as stream:
        content = stream.read(MAX_RULE_BYTES + 1)
    return parse_rules(content, 'json' if path.suffix.lower() == '.json' else 'yaml')


def dump_rules(document):
    """Canonical YAML preserves meaning/descriptions, not comments/formatting."""
    return yaml.safe_dump(validate_rules(document), sort_keys=False, allow_unicode=True)


def validate_facts(document, facts):
    """Missing or null observations are errors, never healthy zero/false values."""
    _bounded_json(facts)
    _require(isinstance(facts, dict) and set(facts) == set(document['inputs']),
             'Facts must contain exactly the declared inputs')
    for field, kind in document['inputs'].items():
        _scalar(facts[field], kind, f'Input {field}')
    return copy.deepcopy(facts)


def simple_editor_supported(document):
    """A future form supports leaves and flat all groups; nested logic stays YAML."""
    document = validate_rules(document)
    if document.get('checks'):
        return False
    for rule in document['rules']:
        condition = rule['when']
        if 'field' in condition:
            continue
        if set(condition) != {'all'} or any('field' not in child for child in condition['all']):
            return False
    return True


def replace_from_simple_editor(current, proposed):
    """Reject lossy editing of advanced documents; no persistence or activation."""
    _require(simple_editor_supported(current), 'Advanced rule requires the YAML editor')
    _require(simple_editor_supported(proposed), 'Proposed rule exceeds the simple editor')
    return validate_rules(proposed)
