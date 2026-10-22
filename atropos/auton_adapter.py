"""Optional adapter to an explicitly configured Auton OperationService."""
import asyncio
import hashlib
import threading
import time

from .actions import Action, ActionFailed, OutcomeUnknown

AUTON_CONNECTION = 'bound-target'


def auton_action(target, uris, endpoint, auth=None, service_factory=None, transport=None, client_factory=None):
    """Bind an action to one trusted destination/endpoint outside the workflow.

    Requires an Auton version exposing auton_client.operations.OperationService.
    No CLI subprocess and no URLs or credentials taken from workflow inputs.
    """
    if service_factory is None:
        from auton_client.operations import OperationService
        service_factory = OperationService

    async def execute(params, context):
        if context.test_mode:
            raise ActionFailed("Auton test execution adapter is not configured")
        if context.target != target:
            raise ActionFailed('target does not match configured action')
        stopped = threading.Event()
        job_id = hashlib.sha256(context.idempotency_key.encode()).hexdigest()
        try:
            service = service_factory(
                targets={AUTON_CONNECTION: {'uris': uris}}, endpoint=endpoint, payload=params, auth=auth,
                parallel=1, timeout=context.timeout,
                http_timeout=min(5, context.timeout),
                **({'transport': transport} if transport else {}),
                **({'client_factory': client_factory} if client_factory else {}))
        except Exception as exc:
            raise ActionFailed('invalid Auton action configuration') from exc
        try:
            result = await asyncio.to_thread(
                service.execute_target, AUTON_CONNECTION, job_id,
                time.monotonic() + context.timeout, stopped, output_limit=0)
        finally:
            # This stops local observation, not the remote job.
            stopped.set()
        if not isinstance(result, dict):
            raise OutcomeUnknown()
        status, code = result.get('status'), result.get('return_code')
        if status == 'completed' and type(code) is int and code == 0:
            return {'job_id': job_id, 'status': 'completed', 'return_code': 0}
        if status in ('rejected', 'not_submitted') or (
                status == 'failed' and type(code) is int and code != 0):
            raise ActionFailed('Auton action failed')
        raise OutcomeUnknown()

    # Even a confirmed failure could have partial effects; retries are disabled.
    return Action(execute, retry_safe=False)
