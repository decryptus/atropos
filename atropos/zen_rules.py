"""Compile the shared rule model to JDM and evaluate with the real ZEN engine."""
import asyncio
import hashlib
import json
import threading

from .actions import Action, ActionFailed
from .rule_model import RuleError, expand_condition, validate_facts, validate_rules

COMPILER_VERSION = 'atropos-rules-v1.1'
ZEN_COMPARISONS = {'eq': '==', 'ne': '!=', 'gt': '>', 'gte': '>=', 'lt': '<', 'lte': '<='}


def _literal(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _expression(condition, constants):
    if 'all' in condition or 'any' in condition:
        group = 'all' if 'all' in condition else 'any'
        joiner = ' and ' if group == 'all' else ' or '
        return '(' + joiner.join(_expression(child, constants) for child in condition[group]) + ')'
    if 'not' in condition:
        return '(not ' + _expression(condition['not'], constants) + ')'
    field = 'facts[' + _literal(condition['field']) + ']'
    operator = next(key for key in condition if key != 'field')
    value = f'rule_constants[{len(constants)}]'
    constants.append(condition[operator])
    if operator in ('in', 'not_in'):
        result = f'contains({value}, {field})'
        return f'(not {result})' if operator == 'not_in' else f'({result})'
    return f'({field} {ZEN_COMPARISONS[operator]} {value})'


def compile_jdm(document):
    document = validate_rules(document)
    decision = _literal(document['default'])
    matched = _literal('__default__')
    constants = []
    # Nested ternaries preserve explicit first-match ordering.
    for rule in reversed(document['rules']):
        predicate = _expression(expand_condition(rule['when'], document.get('checks', {})), constants)
        decision = f'({predicate} ? {_literal(rule["then"])} : {decision})'
        matched = f'({predicate} ? {_literal(rule["id"])} : {matched})'
    fingerprint = hashlib.sha256(json.dumps(
        document, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return {
        'contentType': 'application/vnd.gorules.decision',
        'atropos': {'compiler': COMPILER_VERSION, 'source_sha256': fingerprint, 'constants': constants},
        'nodes': [
            {'id': 'input', 'type': 'inputNode', 'name': 'Facts', 'position': {'x': 0, 'y': 0}},
            {'id': 'rules', 'type': 'expressionNode', 'name': document['name'],
             'position': {'x': 250, 'y': 0}, 'content': {
                 'passThrough': False,
                 'expressions': [
                     {'id': 'decision', 'key': 'decision', 'value': decision},
                     {'id': 'matched_rule', 'key': 'matched_rule', 'value': matched},
                 ],
             }},
            {'id': 'output', 'type': 'outputNode', 'name': 'Decision', 'position': {'x': 500, 'y': 0}},
        ],
        'edges': [
            {'id': 'input-rules', 'type': 'edge', 'sourceId': 'input', 'targetId': 'rules'},
            {'id': 'rules-output', 'type': 'edge', 'sourceId': 'rules', 'targetId': 'output'},
        ],
    }


class ZenRules:
    def __init__(self, document):
        self._document = validate_rules(document)
        self._jdm = compile_jdm(self._document)
        try:
            import zen
        except ImportError as exc:
            raise RuleError('Install the optional zen dependency: pip install .[zen]') from exc
        try:
            self._engine = zen.ZenEngine()
            self._decision = self._engine.create_decision(json.dumps(self._jdm, ensure_ascii=False))
        except Exception as exc:
            raise RuleError('ZEN could not load the compiled model') from exc
        self._lock = threading.Lock()
        self._labels = {rule['then'] for rule in self._document['rules']} | {self._document['default']}
        self._rule_ids = {rule['id'] for rule in self._document['rules']} | {'__default__'}

    def evaluate(self, facts):
        facts = validate_facts(self._document, facts)
        try:
            # Do not assume a shared native decision supports concurrent calls.
            with self._lock:
                response = self._decision.evaluate({'facts': facts, 'rule_constants': self._jdm['atropos']['constants']})
            result = response['result']
            if (not isinstance(result, dict) or set(result) != {'decision', 'matched_rule'}
                    or result['decision'] not in self._labels
                    or result['matched_rule'] not in self._rule_ids):
                raise ValueError('invalid result')
            return result
        except Exception as exc:
            # Never expose native exceptions containing expressions or inputs.
            raise RuleError('ZEN evaluation failed') from exc


def zen_rule_action(document):
    """Bind a validated decision model to a trusted registry entry before admission."""
    evaluator = ZenRules(document)

    async def execute(params, context):
        try:
            return await asyncio.to_thread(evaluator.evaluate, params)
        except RuleError as exc:
            raise ActionFailed('Rule evaluation failed') from exc

    return Action(execute, retry_safe=True)
