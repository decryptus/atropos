"""Opt-in test receiver adapter. Destination and credentials are host-owned."""
import asyncio
import copy
import hashlib
from pathlib import Path
import re
import sys

from .actions import Action, OutcomeUnknown, simulate_alert, unavailable_alert
from .definitions import encode
from .worker import ExecutionDenied

REQUIRED = frozenset({'backend', 'url', 'token_file'})
OPTIONAL = frozenset({'timeout', 'allow_loopback_http', 'ca_file'})
MAX_ENVELOPE_BYTES = 65536
MAX_CA_BYTES = 1048576
MAX_REQUEST_BYTES = MAX_ENVELOPE_BYTES + 2 * MAX_CA_BYTES
MAX_ACK_BYTES = 4096
RECEIVER_PATH = '/v1/test-alerts'
TEST_ID = re.compile(r'[0-9a-f]{64}')


def validate_delivery(value):
    """No network or secret reads while validating the private service profile."""
    from atropos_client.config import origin
    if (type(value) is not dict or not REQUIRED <= set(value)
            or not set(value) <= REQUIRED | OPTIONAL or value['backend'] != 'webhook'
            or type(value.get('allow_loopback_http', False)) is not bool
            or type(value.get('timeout', 5)) is not int
            or not 1 <= value.get('timeout', 5) <= 30
            or not isinstance(value['token_file'], str) or not 0 < len(value['token_file']) <= 4096):
        raise ValueError('invalid_test_alert_delivery')
    destination = origin(value['url'], value.get('allow_loopback_http', False))
    if 'ca_file' in value and (type(value['ca_file']) is not str or not 0 < len(value['ca_file']) <= 4096
                              or value.get('allow_loopback_http', False)):
        raise ValueError('invalid_test_alert_ca')
    return dict(value, url=destination, timeout=value.get('timeout', 5),
                allow_loopback_http=value.get('allow_loopback_http', False))


def delivery_envelope(alert, context):
    if (context.test_mode is not True or not isinstance(context.test_id, str)
            or TEST_ID.fullmatch(context.test_id) is None or context.run_id != context.test_id):
        raise ExecutionDenied('test_context_required')
    delivery_id = hashlib.sha256(context.idempotency_key.encode()).hexdigest()
    return {'version': 1, 'test_context': {'test': True, 'id': context.test_id},
            'delivery_id': delivery_id, 'run_id': context.run_id, 'step': context.step,
            'target': context.target, 'alert': alert}


async def send_to_receiver(configuration, token, envelope, timeout, ca=None):
    # The child has no retry path. Killing it bounds DNS/TLS/slow-body handling;
    # cancellation cannot prove the receiver did not already accept the request.
    request = encode({'configuration': configuration, 'token': token,
                      'envelope': envelope, 'ca': ca}, MAX_REQUEST_BYTES).encode()
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-m', 'atropos.test_alert_worker',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(process.communicate(request), timeout=timeout)
        if process.returncode != 0 or out != b'{"accepted":true}\n':
            raise OutcomeUnknown('test_alert_outcome_unknown')
    except (OSError, asyncio.TimeoutError):
        raise OutcomeUnknown('test_alert_outcome_unknown') from None
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


def configured_test_alert(configuration_loader, config_path):
    # A running worker may not silently switch from simulation to delivery, or
    # switch receivers. Config changes require an explicit worker restart.
    initial = copy.deepcopy(configuration_loader()[0].get('test_alert_delivery'))
    pinned = validate_delivery(initial) if initial is not None else None

    async def simulate(params, context):
        try:
            current = configuration_loader()[0].get('test_alert_delivery')
            current = validate_delivery(current) if current is not None else None
            if current != pinned:
                raise ValueError('test_alert_destination_changed')
            result = await simulate_alert(params, context)
            envelope = delivery_envelope(result['alert'], context)
            encode(envelope, MAX_ENVELOPE_BYTES)
            if current is None:
                return result
            from atropos_client.client import read_token
            path = Path(current['token_file'])
            if not path.is_absolute():
                path = Path(config_path).parent / path
            token = read_token(path)
            ca = None
            if 'ca_file' in current:
                from atropos_client.files import read_private
                import ssl
                ca_path = Path(current['ca_file'])
                if not ca_path.is_absolute(): ca_path = Path(config_path).parent / ca_path
                ca = read_private(ca_path, MAX_CA_BYTES).decode()
                ssl.create_default_context(cadata=ca)  # Invalid trust material is a known pre-send refusal.
        except Exception:
            raise ExecutionDenied('test_alert_configuration_denied') from None
        await send_to_receiver(current, token, envelope, min(context.timeout, current['timeout']), ca)
        # Do not retain large payloads or credentials/URLs in the delivery receipt.
        return {'alert': {'test': True, 'test_id': context.test_id,
                'test_context': envelope['test_context'], 'target': context.target,
                'subject': result['alert']['subject'][:256],
                'delivery_id': envelope['delivery_id'], 'delivery': 'test_receiver_acknowledged'}}

    return Action(unavailable_alert, False, simulate)
