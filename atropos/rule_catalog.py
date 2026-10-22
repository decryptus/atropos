"""Versioned offline starting points; instantiation never activates or runs jobs."""
import copy
import math

from .rule_model import RuleError, validate_rules

CATALOG_VERSION = 1
# Defaults are editable examples, not production recommendations.
_TEMPLATES = {
    'cpu-high': ('CPU usage', 'cpu_percent', 'number', 'gte', 90),
    'memory-high': ('Memory usage', 'memory_percent', 'number', 'gte', 90),
    'disk-high': ('Disk space usage', 'disk_percent', 'number', 'gte', 90),
    'inodes-high': ('Inode usage', 'inode_percent', 'number', 'gte', 90),
    'directory-unreadable': ('Directory read access unavailable', 'readable', 'boolean', 'eq', False),
    'directory-unwritable': ('Directory write access unavailable', 'writable', 'boolean', 'eq', False),
    'container-unhealthy': ('Container health check failed', 'health_status', 'string', 'eq', 'unhealthy'),
    'restarts-high': ('Restart count in the supplied observation window', 'restart_count', 'integer', 'gte', 3),
}


def list_templates():
    return {
        'catalog_version': CATALOG_VERSION,
        'templates': [
            {'id': key, 'description': label, 'input': field, 'type': kind,
             'parameters': {'threshold': default} if operator == 'gte' else {}}
            for key, (label, field, kind, operator, default) in _TEMPLATES.items()
        ],
    }


def instantiate(template_ids, *, name='starter-policy', match='any', parameters=None,
                decision='review', catalog_version=CATALOG_VERSION):
    """Return a self-contained draft with concrete checks; no runtime catalog lookup.

    parameters maps template IDs to {threshold: number}. Decisions are labels,
    never action names. The caller owns authorization, persistence and activation.
    """
    if type(catalog_version) is not int or catalog_version != CATALOG_VERSION:
        raise RuleError('Unsupported catalog version')
    if (not isinstance(template_ids, (list, tuple)) or not 1 <= len(template_ids) <= len(_TEMPLATES)
            or any(not isinstance(key, str) or key not in _TEMPLATES for key in template_ids)
            or len(set(template_ids)) != len(template_ids)):
        raise RuleError('Select distinct known template IDs')
    if match not in ('all', 'any'):
        raise RuleError('Template combination must use all or any')
    parameters = {} if parameters is None else parameters
    if not isinstance(parameters, dict) or not set(parameters) <= set(template_ids):
        raise RuleError('Parameters must target selected templates')
    inputs, checks = {}, {}
    for key in template_ids:
        _, field, kind, operator, default = _TEMPLATES[key]
        options = parameters.get(key, {})
        allowed = {'threshold'} if operator == 'gte' else set()
        if not isinstance(options, dict) or not set(options) <= allowed:
            raise RuleError('Unsupported template parameter')
        threshold = options.get('threshold', default)
        if operator == 'gte':
            valid = (type(threshold) in (int, float) and 0 <= threshold <= 100
                     and math.isfinite(threshold)) if kind == 'number' else (
                         type(threshold) is int and 1 <= threshold <= 2 ** 53 - 1)
            if not valid:
                raise RuleError('Threshold requires 0–100 percent or a positive integer count')
        inputs[field] = kind
        checks[key] = {'field': field, operator: copy.deepcopy(threshold)}
    return validate_rules({
        'version': 1, 'name': name,
        'description': f'Draft from Atropos catalog v{catalog_version}; review thresholds and scope before activation.',
        'inputs': inputs, 'checks': checks,
        'rules': [{'id': 'combined', 'when': {match: [{'ref': key} for key in template_ids]},
                   'then': decision}],
        'default': 'ignore',
    })
