"""Public configuration uses roles, with no product-specific parameter names."""
import json
import os
from .backend_registry import validate_configuration


def backend_settings(environ=None):
    env = os.environ if environ is None else environ
    if 'ATROPOS_REDIS_URL' in env:
        raise ValueError('unsupported_configuration_use_storage_role')
    specs = {}
    for role, default in (('storage', 'sqlite'), ('queue', 'storage')):
        prefix = 'ATROPOS_' + role.upper() + '_'
        spec = {'backend': env.get(prefix + 'BACKEND', default)}
        if prefix + 'URL' in env: spec['url'] = env[prefix + 'URL']
        if prefix + 'OPTIONS' in env:
            spec['options'] = json.loads(env[prefix + 'OPTIONS'])
        if role == 'storage' and spec['backend'] == 'sqlite' and 'url' not in spec:
            spec['url'] = 'sqlite:///atropos.db'
        specs[role] = validate_configuration(role, spec)
    return specs
