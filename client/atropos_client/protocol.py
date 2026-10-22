"""Finite remote rule API, independent of optional Atropos dependencies."""
import inspect
import json

from .api import RuleAPI
from .json_codec import unique_pairs as _unique_pairs, finite_float, reject_constant

ROUTE = '/v1/rules/command'
MAX_REQUEST_BYTES = 524288
MAX_RESPONSE_BYTES = 16777216
METHODS = frozenset(('catalog', 'create_from_templates', 'save_draft', 'import_draft',
    'availability', 'archive', 'unarchive', 'delete', 'read', 'draft', 'export_yaml', 'history', 'test', 'test_report', 'active',
    'activate', 'rollback', 'deactivate', 'audit', 'evaluate_active', 'observe', 'plan_dispatch', 'dispatch', 'dispatch_status', 'search_interventions', 'observation_challenge', 'plan_observed_dispatch',
    'dispatch_observed', 'observe_dispatch_plan', 'observe_dispatch', 'simulate_dispatch', 'test_dispatch', 'test_dispatch_status', 'test_bindings', 'plan_reconciliation', 'reconcile'))
MUTATIONS = frozenset(('archive', 'unarchive', 'delete', 'create_from_templates', 'save_draft', 'import_draft', 'test',
                       'activate', 'rollback', 'deactivate', 'dispatch', 'observation_challenge', 'dispatch_observed', 'observe_dispatch', 'test_dispatch', 'reconcile'))
ENGINE_OBSERVATION_METHODS = frozenset(('observation_challenge', 'plan_observed_dispatch', 'dispatch_observed'))
GATEWAY_OBSERVATION_METHODS = frozenset(('observe_dispatch_plan', 'observe_dispatch'))
TOKEN_PATTERN = r'[A-Za-z0-9_-]{43,128}'
ERROR_CODES = frozenset(('rule_access_denied', 'rule_conflict', 'rule_not_found',
    'invalid_rule_request', 'rule_service_unavailable', 'rule_outcome_unknown', 'rule_queue_full'))


def _observe(self, name, target):
    pass


def arguments(method, *args, **kwargs):
    if method not in METHODS:
        raise ValueError('invalid_rule_request')
    function = _observe if method == 'observe' else getattr(RuleAPI, method)
    bound = inspect.signature(function).bind(None, *args, **kwargs)
    return {key: value for key, value in bound.arguments.items() if key != 'self'}


def decode(raw):
    return json.loads(raw, object_pairs_hook=_unique_pairs,
                      parse_constant=reject_constant, parse_float=finite_float)


def request(raw, *, allow_expected_actor=False):
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError('invalid_rule_request')
    value = decode(raw)
    fields = {'method', 'arguments'}
    if type(value) is not dict or not fields <= set(value):
        raise ValueError('invalid_rule_request')
    allowed = fields | ({'expected_actor'} if allow_expected_actor else set())
    if (not set(value) <= allowed or type(value['method']) is not str
            or type(value['arguments']) is not dict):
        raise ValueError('invalid_rule_request')
    expected = value.get('expected_actor')
    if 'expected_actor' in value and (type(expected) is not dict
            or set(expected) != {'subject', 'organization'}
            or not all(isinstance(v, str) and 0 < len(v) <= 128 for v in expected.values())):
        raise ValueError('invalid_expected_actor')
    method = value['method']
    return method, arguments(method, **value['arguments']), expected


def command(raw):
    method, values, _ = request(raw)
    return method, values
