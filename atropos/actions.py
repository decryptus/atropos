"""Trusted action registry; workflows can select only pre-registered actions."""
from dataclasses import dataclass
from typing import Awaitable, Callable



class ActionFailed(Exception):
    """A confirmed failure; optional retry is safe only for approved adapters."""


class OutcomeUnknown(Exception):
    """The external effect may have occurred. Never retry automatically."""


@dataclass(frozen=True)
class Context:
    run_id: str
    step: str
    target: str
    attempt: int
    timeout: float
    test_mode: bool = False
    test_id: str | None = None

    @property
    def idempotency_key(self):
        return f"{self.run_id}:{self.step}"


@dataclass(frozen=True)
class Action:
    execute: Callable[[dict, Context], Awaitable[dict]]
    retry_safe: bool = False
    simulate: Callable[[dict, Context], Awaitable[dict]] | None = None
    configuration_revision: str | None = None


async def echo(params, context):
    return params


async def fail(params, context):
    raise ActionFailed("demonstration failure")


async def unavailable_alert(params, context):
    raise ActionFailed('alert_delivery_not_configured')


async def simulate_alert(params, context):
    if not context.test_mode or context.test_id is None:
        raise ActionFailed('test_context_required')
    return {'alert': {'test': True, 'test_id': context.test_id, 'target': context.target,
                     'test_context': {'test': True, 'id': context.test_id},
                     'subject': '[TEST] ' + str(params.get('subject', 'Workflow notification')),
                     'payload': params, 'delivery': 'simulated'}}


def builtin_actions():
    # No shell, arbitrary URL, filesystem access or dynamic import from rules.
    return {'core.echo': Action(echo, True, echo), 'core.fail': Action(fail, True, fail),
            'core.alert': Action(unavailable_alert, False, simulate_alert)}

