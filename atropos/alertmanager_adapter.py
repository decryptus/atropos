"""Explicit, revision-pinned Alertmanager action; test transport stays separate."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import re
import sys

from atropos_client.config import origin
from atropos_client.client import read_token
from atropos_client.files import read_private
from .actions import Action, OutcomeUnknown
from .definitions import encode
from .execution_adapters import adapter_revision, local_file, MAX_CA_BYTES
from .worker import ExecutionDenied

REQUIRED = frozenset({'backend', 'target', 'origin', 'token_file', 'labels'})
OPTIONAL = frozenset({'ca_file', 'allow_loopback_http', 'timeout', 'lifetime_seconds', 'simulation'})
LABEL = re.compile(r'[a-zA-Z_][a-zA-Z0-9_]{0,127}')
MAX_REQUEST_BYTES = 2 * MAX_CA_BYTES
MAX_RESPONSE_BYTES = 4096
API_PATH = '/api/v2/alerts'


def validate_spec(spec):
    if (type(spec) is not dict or not REQUIRED <= set(spec) <= REQUIRED | OPTIONAL
            or spec['backend'] != 'alertmanager' or type(spec['target']) is not str
            or not 1 <= len(spec['target']) <= 1024
            or type(spec.get('allow_loopback_http', False)) is not bool
            or origin(spec['origin'], spec.get('allow_loopback_http', False)) != spec['origin']
            or spec.get('simulation', 'unsupported') not in ('unsupported', 'test_receiver')):
        raise ValueError('invalid_alertmanager_adapter')
    for key in ('token_file', 'ca_file'):
        if key in spec and (type(spec[key]) is not str or not 0 < len(spec[key]) <= 4096):
            raise ValueError('invalid_alertmanager_file')
    if 'ca_file' in spec and spec.get('allow_loopback_http', False):
        raise ValueError('tls_requires_https')
    for key, default, maximum in (('timeout', 5, 30), ('lifetime_seconds', 300, 3600)):
        value = spec.get(key, default)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError('invalid_alertmanager_limits')
    labels = spec['labels']
    if (type(labels) is not dict or not 1 <= len(labels) <= 32 or not labels.get('alertname')
            or any(type(k) is not str or not LABEL.fullmatch(k) or k.startswith(('atropos_', '__'))
                   or type(v) is not str or not 0 < len(v) <= 1024 or not v.isprintable()
                   for k, v in labels.items())):
        raise ValueError('invalid_alertmanager_labels')
    return spec


def alert_payload(spec, params, context):
    if (context.test_mode is not False or context.test_id is not None or context.target != spec['target']
            or type(context.run_id) is not str or not re.fullmatch(r'[0-9a-f]{64}', context.run_id)):
        raise ExecutionDenied('invalid_alertmanager_context')
    if (type(params) is not dict or not set(params) <= {'subject', 'description'}
            or any(type(v) is not str or not 0 < len(v) <= 4096 for v in params.values())):
        raise ExecutionDenied('invalid_alertmanager_content')
    delivery_id = hashlib.sha256(context.idempotency_key.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    payload = [{'labels': dict(spec['labels'], atropos_target=context.target,
                              atropos_delivery_id=delivery_id),
                'annotations': {'summary': params.get('subject', 'Intervention notification'),
                                'description': params.get('description', ''),
                                'atropos_run_id': context.run_id, 'atropos_step': context.step},
                'startsAt': now.isoformat(),
                'endsAt': (now + timedelta(seconds=spec.get('lifetime_seconds', 300))).isoformat()}]
    return payload, delivery_id


async def send(request, timeout):
    raw = encode(request, MAX_REQUEST_BYTES).encode()
    process = None
    try:
        process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'atropos.alertmanager_worker',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(process.communicate(raw), timeout=timeout)
        if process.returncode != 0 or out != b'{"accepted":true}\n':
            raise OutcomeUnknown('alertmanager_outcome_unknown')
    except (OSError, asyncio.TimeoutError):
        raise OutcomeUnknown('alertmanager_outcome_unknown') from None
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


def configured_alertmanager(action_name, original, config_path, configuration_loader):
    spec = copy.deepcopy(validate_spec(original))

    def unchanged():
        if configuration_loader()[0].get('execution_adapters', {}).get(action_name) != spec:
            raise ExecutionDenied('alertmanager_configuration_changed')

    async def execute(params, context):
        try:
            unchanged()
            payload, delivery_id = alert_payload(spec, params, context)
            token = read_token(local_file(config_path, spec['token_file']))
            ca = read_private(local_file(config_path, spec['ca_file']), MAX_CA_BYTES).decode() if 'ca_file' in spec else None
            request = dict(configuration=spec, token=token, ca=ca, payload=payload)
            encode(request, MAX_REQUEST_BYTES)
            unchanged()
        except Exception:
            raise ExecutionDenied('alertmanager_configuration_denied') from None
        await send(request, min(context.timeout, spec.get('timeout', 5)))
        return {'alert': {'delivery': 'alertmanager_accepted', 'delivery_id': delivery_id}}

    simulate = None
    if spec.get('simulation') == 'test_receiver':
        from .test_alert_delivery import configured_test_alert
        test_action = configured_test_alert(configuration_loader, config_path)

        async def simulate(params, context):
            unchanged()
            if context.target != spec['target']:
                raise ExecutionDenied('invalid_alertmanager_target')
            return await test_action.simulate(params, context)

    return Action(execute, retry_safe=False, simulate=simulate, configuration_revision=adapter_revision(spec))
