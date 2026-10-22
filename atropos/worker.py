"""Execute one persisted step at a time; never replay an uncertain effect."""
import asyncio
import copy
import uuid
from contextlib import suppress

from .actions import ActionFailed, Context, OutcomeUnknown
from .definitions import DefinitionError, encode, resolve, validate

MAX_RESULT_BYTES = 8192


class ExecutionDenied(Exception):
    """Known pre-dispatch refusal: no action started in this attempt."""



class Worker:
    def __init__(self, store, actions, consumer=None, *, authorizer=None, require_authorization=False, test_mode=False):
        self.store = store
        self.actions = dict(actions)
        self.consumer = consumer or f'worker-{uuid.uuid4().hex}'
        self.cursor = '0-0'
        self.test_mode = test_mode
        self.authorizer = authorizer
        self.require_authorization = require_authorization

    async def _heartbeat(self, claim, lost):
        try:
            while True:
                await asyncio.sleep(self.store.lease_ms / 3000)
                if not await asyncio.to_thread(self.store.heartbeat, claim):
                    lost.set()
                    return
        except Exception:
            # Do not assume ownership after a storage failure.
            lost.set()

    async def run_once(self, block_ms=1000):
        message, self.cursor = await asyncio.to_thread(
            self.store.receive, self.consumer, self.cursor, block_ms)
        if message is None:
            return False
        message_id, fields = message
        claim = await asyncio.to_thread(
            self.store.claim, message_id, fields['run_id'], self.consumer)
        if claim is None:
            return True
        state = copy.deepcopy(claim.state)
        key = state['current']
        lost = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(claim, lost))
        action_task = loss_task = None
        delay = 0
        try:
            try:
                workflow = validate(state['workflow'], self.actions)
                step = workflow['steps'][key]
                params = resolve(step.get('with', {}), {
                    'input': state['input'], 'results': state['results'],
                    'target': state['target'], 'run_id': state['id'],
                })
                encode(params)
            except (DefinitionError, KeyError):
                state.update(status='failed', error='invalid_step_input_or_definition')
            else:
                context = Context(state['id'], key, state['target'],
                                  state['attempts'][key], step.get('timeout', 30),
                                  self.test_mode, state.get('test_context', {}).get('id'))
                action = self.actions[step['action']]
                try:
                    if self.test_mode != ('test_context' in state):
                        raise ExecutionDenied()
                    # Attributed/admitted work never runs on an unguarded worker.
                    if self.test_mode or self.require_authorization or 'attribution' in state or 'execution' in state:
                        try:
                            if self.authorizer is None or await asyncio.to_thread(
                                    self.authorizer, copy.deepcopy(state)) is not True:
                                raise ExecutionDenied()
                        except Exception:
                            raise ExecutionDenied() from None
                    # Recheck the lease immediately before dispatch as well as
                    # before committing. An external executor still needs its
                    # own idempotency/authorization protections.
                    if not await asyncio.to_thread(self.store.heartbeat, claim):
                        return True
                    execute = action.simulate if self.test_mode else action.execute
                    if execute is None:
                        raise ExecutionDenied()
                    action_task = asyncio.create_task(execute(params, context))
                    loss_task = asyncio.create_task(lost.wait())
                    done, _ = await asyncio.wait(
                        {action_task, loss_task}, timeout=context.timeout,
                        return_when=asyncio.FIRST_COMPLETED)
                    if lost.is_set():
                        return True  # Expired lease will be quarantined by recovery.
                    if action_task not in done:
                        raise OutcomeUnknown()
                    result = action_task.result()
                    if not isinstance(result, dict):
                        raise OutcomeUnknown()
                    if self.test_mode:
                        result = dict(result, _test_context=dict(state['test_context']))
                    encode(result, MAX_RESULT_BYTES)
                except ExecutionDenied:
                    # Do not retry or follow an error branch after authorization refusal.
                    state.update(status='cancelled', error='execution_authorization_denied')
                except ActionFailed:
                    state['error'] = 'action_failed'
                    retry = step.get('retry', {})
                    if action.retry_safe and context.attempt < retry.get('attempts', 1):
                        state['status'] = 'retrying'
                        delay = retry.get('delay', 1)
                    elif step.get('on_error'):
                        state.update(status='queued', current=step['on_error'])
                    else:
                        state['status'] = 'failed'
                except Exception:
                    # Timeouts, unexpected exceptions and invalid/oversized
                    # outputs cannot prove that the external effect did not run.
                    state.update(status='uncertain', error='action_outcome_unknown')
                else:
                    state['results'][key] = result
                    state.pop('error', None)
                    next_step = step.get('next')
                    branches = step.get('on_decision', {})
                    if branches:
                        decision = result.get('decision')
                        if not isinstance(decision, str) or decision not in branches:
                            state.update(status='failed', error='unmatched_decision')
                        else:
                            next_step = branches[decision]
                            state.update(status='queued' if next_step else 'succeeded')
                    else:
                        state.update(status='queued' if next_step else 'succeeded')
                    if state['status'] == 'queued':
                        state['current'] = next_step
            state['history'].append({
                'step': key, 'status': state['status'], 'error': state.get('error'),
                'at': await asyncio.to_thread(self.store.now),
                **({'test_context': dict(state['test_context'])} if self.test_mode else {}),
            })
            await asyncio.to_thread(self.store.finish, claim, state, delay)
            return True
        finally:
            for task in (action_task, loss_task, heartbeat):
                if task is not None:
                    task.cancel()
            for task in (action_task, loss_task, heartbeat):
                if task is not None:
                    with suppress(asyncio.CancelledError, Exception):
                        await task

    async def run(self):
        while True:
            if not await self.run_once():
                # A paused backend returns immediately; avoid a maintenance busy loop.
                await asyncio.sleep(.1)
