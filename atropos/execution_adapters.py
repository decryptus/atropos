"""Trusted engine-owned execution composition; no arbitrary imports from YAML."""
import copy
import hashlib
from functools import partial
import json
from pathlib import Path

from atropos_client.config import origin
from atropos_client.files import read_private
from .actions import Action, ActionFailed, builtin_actions
from .authorization import identifier
from .definitions import name

MAX_ADAPTERS = 64
MAX_PASSWORD_BYTES = 4096
MAX_CA_BYTES = 1048576
REQUIRED = frozenset({'backend', 'target', 'origin', 'endpoint', 'username', 'password_file'})
OPTIONAL = frozenset({'allow_loopback_http', 'ca_file', 'simulation'})
SIMULATIONS = frozenset({'unsupported', 'noop'})


def validate_adapters(specs, resources):
    if type(specs) is not dict or len(specs) > MAX_ADAPTERS:
        raise ValueError('invalid_execution_adapters')
    targets = {'/'.join((r['organization'], r['kind'], r['id'])) for r in resources
               if r['kind'] in ('server', 'container')}
    for action, spec in specs.items():
        name(action, 'action')
        if action.startswith('core.') or type(spec) is not dict:
            raise ValueError('invalid_execution_adapter')
        if spec.get('backend') == 'alertmanager':
            from .alertmanager_adapter import validate_spec
            validate_spec(spec)
            if spec['target'] not in targets:
                raise ValueError('invalid_execution_target')
            continue
        if not REQUIRED <= set(spec) <= REQUIRED | OPTIONAL:
            raise ValueError('invalid_execution_adapter')
        if spec['backend'] != 'auton' or type(spec['target']) is not str or spec['target'] not in targets:
            raise ValueError('invalid_execution_target')
        allow = spec.get('allow_loopback_http', False)
        if type(allow) is not bool or origin(spec['origin'], allow) != spec['origin']:
            raise ValueError('invalid_execution_origin')
        name(spec['endpoint'], 'endpoint')
        identifier(spec['username'])
        if ':' in spec['username']:
            raise ValueError('invalid_execution_username')
        for key in ('password_file', 'ca_file'):
            if key in spec and (type(spec[key]) is not str or not 0 < len(spec[key]) <= 4096):
                raise ValueError('invalid_execution_file')
        if 'ca_file' in spec and allow:
            raise ValueError('tls_requires_https')
        if spec.get('simulation', 'unsupported') not in SIMULATIONS:
            raise ValueError('invalid_execution_simulator')
    return specs


def adapter_revision(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def local_file(config_path, value):
    path = Path(value)
    return path if path.is_absolute() else Path(config_path).absolute().parent / path


def configured_actions(specs, config_path, configuration_loader):
    if any(spec['backend'] == 'auton' for spec in specs.values()):
        from auton_client.operations import OperationService  # Verify optional dependency before admission.
    actions = builtin_actions()
    for action_name, original in specs.items():
        if original['backend'] == 'alertmanager':
            from .alertmanager_adapter import configured_alertmanager
            actions[action_name] = configured_alertmanager(action_name, original, config_path, configuration_loader)
        else:
            actions[action_name] = configured_action(action_name, copy.deepcopy(original), config_path, configuration_loader)
    return actions


def configured_action(action_name, spec, config_path, configuration_loader):
    revision = adapter_revision(spec)

    def unchanged():
        document, _ = configuration_loader()
        if document.get('execution_adapters', {}).get(action_name) != spec:
            raise ActionFailed('execution_adapter_changed')

    async def execute(params, context):
        # This check also covers the gap between worker authorization and dispatch.
        if context.test_mode or context.target != spec['target']:
            raise ActionFailed('invalid_execution_context')
        session = None
        try:
            unchanged()
            password = read_private(local_file(config_path, spec['password_file']), MAX_PASSWORD_BYTES).decode().rstrip('\r\n')
            if not password or any(ord(c) < 32 or ord(c) == 127 for c in password):
                raise ValueError('invalid_execution_credentials')
            transport = None
            if 'ca_file' in spec:
                ca = local_file(config_path, spec['ca_file'])
                read_private(ca, MAX_CA_BYTES)
                transport = {'verify': str(ca)}
            from .auton_adapter import auton_action
            import requests
            from auton_client.execution import ExecutionClient
            session = requests.Session()
            session.trust_env = False
            adapter = auton_action(spec['target'], [spec['origin']], spec['endpoint'],
                auth=(spec['username'], password), transport=transport,
                client_factory=partial(ExecutionClient, session=session))
            unchanged()
        except Exception:
            if session is not None:
                session.close()
            raise ActionFailed('execution_adapter_unavailable') from None
        try:
            return await adapter.execute(params, context)
        finally:
            session.close()

    async def simulate(params, context):
        if not context.test_mode or not context.test_id or context.target != spec['target']:
            raise ActionFailed('invalid_test_context')
        unchanged()
        return {'simulated': True, 'executor': 'auton', 'effects': 'none',
                'target': context.target, 'endpoint': spec['endpoint'],
                'test_context': {'test': True, 'id': context.test_id}}

    return Action(execute, retry_safe=False,
                  simulate=simulate if spec.get('simulation') == 'noop' else None,
                  configuration_revision=revision)
