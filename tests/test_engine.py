import asyncio
import copy
import unittest

from atropos.actions import Action, ActionFailed, builtin_actions
from atropos.redis_store import Conflict, RedisStore
from atropos.service import WorkflowService
from atropos.worker import Worker
from support import RedisFixture, workflow


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = RedisFixture()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.close()

    def setUp(self):
        self.fixture.client.flushdb()
        self.store = RedisStore(self.fixture.client, lease_ms=300)
        self.store.initialize()
        self.actions = builtin_actions()

    def submit(self, definition=None, target='synthetic', key=None):
        return WorkflowService(self.store, self.actions).submit(
            definition or workflow(), {'value': 42}, target, key)['id']

    def step(self):
        return asyncio.run(Worker(self.store, self.actions).run_once(0))

    def test_steps_resolve_prior_results_and_complete(self):
        run = self.submit(workflow({
            'first': {'action': 'core.echo', 'with': {'answer': {'$ref': '/input/value'}}, 'next': 'second'},
            'second': {'action': 'core.echo', 'with': {'previous': {'$ref': '/results/first/answer'}}},
        }))
        self.step()
        self.assertEqual(self.store.get(run)['status'], 'queued')
        self.step()
        state = self.store.get(run)
        self.assertEqual(state['status'], 'succeeded')
        self.assertEqual(state['results']['second'], {'previous': 42})
        self.assertEqual(state['attempts'], {'first': 1, 'second': 1})
        self.assertFalse(self.fixture.client.exists(self.store.target_key('synthetic')))

    def test_deduplication_and_conflict(self):
        run = self.submit(key='incident-1')
        self.assertEqual(run, self.submit(key='incident-1'))
        self.assertEqual(self.fixture.client.xlen(self.store.stream), 1)
        with self.assertRaises(Conflict):
            self.submit(target='different', key='incident-1')
        self.step()
        self.assertEqual(run, self.submit(key='incident-1'))
        self.assertFalse(self.step())

    def test_decision_branch(self):
        run = self.submit(workflow({
            'first': {'action': 'core.echo', 'with': {'decision': 'repair'},
                      'on_decision': {'repair': 'repair', 'skip': None}},
            'repair': {'action': 'core.echo'},
        }))
        self.step()
        self.step()
        self.assertEqual(self.store.get(run)['attempts'], {'first': 1, 'repair': 1})

    def test_unmatched_decision_fails_without_following_step(self):
        run = self.submit(workflow({
            'first': {'action': 'core.echo', 'on_decision': {'yes': 'second'}},
            'second': {'action': 'core.echo'},
        }))
        self.step()
        self.assertEqual(self.store.get(run)['status'], 'failed')
        self.assertNotIn('second', self.store.get(run)['attempts'])

    def test_confirmed_failure_can_follow_error_branch(self):
        run = self.submit(workflow({
            'first': {'action': 'core.fail', 'on_error': 'report'},
            'report': {'action': 'core.echo'},
        }))
        self.step()
        self.step()
        self.assertEqual(self.store.get(run)['status'], 'succeeded')

    def test_retry_backoff_survives_duplicate_queue_message(self):
        run = self.submit(workflow({'first': {
            'action': 'core.fail', 'retry': {'attempts': 2, 'delay': .1}}}))
        self.step()
        self.fixture.client.xadd(self.store.stream, {'run_id': run})
        self.step()
        self.assertEqual(self.store.get(run)['attempts']['first'], 1)
        async def delayed():
            await asyncio.sleep(.12)
            await Worker(self.store, self.actions).run_once(0)
        asyncio.run(delayed())
        self.assertEqual(self.store.get(run)['status'], 'failed')
        self.assertEqual(self.store.get(run)['attempts']['first'], 2)
        self.assertTrue(self.fixture.client.sismember(self.store.dead, run))

    def test_timeout_is_uncertain_and_blocks_same_target(self):
        calls = []
        async def slow(params, context):
            calls.append(context.run_id)
            await asyncio.sleep(2)
            return {}
        self.actions['slow'] = Action(slow, True)
        definition = workflow({'first': {'action': 'slow', 'timeout': .03,
                                         'retry': {'attempts': 3}}})
        run = self.submit(definition)
        self.step()
        other = self.submit()
        self.step()
        self.assertEqual(self.store.get(run)['status'], 'uncertain')
        self.assertEqual(self.store.get(other)['attempts'], {})
        self.assertEqual(calls, [run])
        self.assertTrue(self.fixture.client.sismember(self.store.quarantine, run))
        self.assertEqual(self.fixture.client.get(self.store.target_key('synthetic')), run)

    def test_expired_inflight_lease_is_quarantined(self):
        run = self.submit()
        message, _ = self.store.receive('dead-worker', block_ms=0)
        self.store.claim(message[0], run, 'dead-worker')
        async def recover():
            await asyncio.sleep(.35)
            await Worker(self.store, self.actions).run_once(0)
        asyncio.run(recover())
        state = self.store.get(run)
        self.assertEqual(state['status'], 'uncertain')
        self.assertEqual(state['results'], {})
        self.assertEqual(state['attempts']['first'], 1)

    def test_heartbeat_preserves_ownership_during_slow_step(self):
        # Observe renewals while the action is held; a 300 ms scheduling race
        # must not masquerade as a failure of the successful-heartbeat contract.
        self.store.lease_ms = 1500
        async def scenario():
            entered, release, renewed = asyncio.Event(), asyncio.Event(), asyncio.Event()
            loop = asyncio.get_running_loop();beats = []
            original = self.store.heartbeat
            def heartbeat(claim):
                result = original(claim)
                if result and entered.is_set():
                    beats.append(loop.time())
                    if len(beats) >= 4:loop.call_soon_threadsafe(renewed.set)
                return result
            self.store.heartbeat = heartbeat
            async def held(params, context):
                entered.set()
                await release.wait()
                return {'done': True}
            self.actions['slow'] = Action(held)
            run = self.submit(workflow({'first': {'action': 'slow', 'timeout': 10}}))
            task = asyncio.create_task(Worker(self.store, self.actions).run_once(0))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                started = loop.time()
                await asyncio.wait_for(renewed.wait(), 8)
                self.assertGreater(loop.time()-started, self.store.lease_ms/1000)
                await Worker(self.store, self.actions).run_once(0)
                self.assertEqual(self.store.get(run)['status'], 'running')
                self.assertEqual(self.store.get(run)['attempts']['first'], 1)
            finally:
                release.set()
                await asyncio.wait_for(task, 5)
                self.store.heartbeat = original
            self.assertEqual(self.store.get(run)['status'], 'succeeded')
            self.assertEqual(self.store.get(run)['results']['first'], {'done': True})
        asyncio.run(scenario())

    def test_stale_worker_cannot_commit(self):
        run = self.submit()
        message, _ = self.store.receive('old', block_ms=0)
        claim = self.store.claim(message[0], run, 'old')
        self.fixture.client.delete(self.store.lease(run))
        state = copy.deepcopy(claim.state)
        state['status'] = 'succeeded'
        self.assertFalse(self.store.finish(claim, state))
        self.assertEqual(self.store.get(run)['status'], 'running')

    def test_same_target_is_serialized(self):
        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event()
            async def held(params, context):
                entered.set()
                await release.wait()
                return {}
            self.actions['held'] = Action(held)
            first = self.submit(workflow({'first': {'action': 'held'}}))
            second = self.submit()
            task = asyncio.create_task(Worker(self.store, self.actions).run_once(0))
            await entered.wait()
            await Worker(self.store, self.actions).run_once(0)
            self.assertEqual(self.store.get(second)['attempts'], {})
            release.set()
            await task
            self.fixture.client.zadd(self.store.delayed, {second: 0})
            await Worker(self.store, self.actions).run_once(0)
            self.assertEqual(self.store.get(first)['status'], 'succeeded')
            self.assertEqual(self.store.get(second)['status'], 'succeeded')
        asyncio.run(scenario())

    def test_operator_resolution_terminates_and_releases(self):
        async def unknown(params, context):
            raise RuntimeError('must not leak this detail')
        self.actions['unknown'] = Action(unknown)
        run = self.submit(workflow({'first': {'action': 'unknown'}}))
        self.step()
        self.assertNotIn('must not leak', str(self.store.get(run)))
        state = self.store.resolve(run, 'failed')
        self.assertEqual(state['status'], 'failed')
        self.assertFalse(self.fixture.client.exists(self.store.target_key('synthetic')))
        with self.assertRaises(Conflict):
            self.store.resolve(run, 'succeeded')

    def test_missing_input_fails_before_action(self):
        run = self.submit(workflow({'first': {'action': 'core.echo',
                                            'with': {'x': {'$ref': '/input/missing'}}}}))
        self.step()
        self.assertEqual(self.store.get(run)['status'], 'failed')
        self.assertEqual(self.store.get(run)['results'], {})

    def test_oversized_result_is_uncertain(self):
        async def large(params, context):
            return {'value': 'x' * 9000}
        self.actions['large'] = Action(large)
        run = self.submit(workflow({'first': {'action': 'large'}}))
        self.step()
        self.assertEqual(self.store.get(run)['status'], 'uncertain')

    def test_checkpoint_survives_redis_restart(self):
        run = self.submit(workflow({
            'first': {'action': 'core.echo', 'next': 'second'},
            'second': {'action': 'core.echo'},
        }), key='durable')
        self.step()
        self.fixture.restart()
        self.step()
        state = self.store.get(run)
        self.assertEqual(state['status'], 'succeeded')
        self.assertEqual(state['attempts'], {'first': 1, 'second': 1})

    def test_unclaimed_pending_message_can_be_recovered(self):
        run = self.submit()
        self.store.receive('gone-before-claim', block_ms=0)
        async def recover():
            await asyncio.sleep(.35)
            await Worker(self.store, self.actions).run_once(0)
        asyncio.run(recover())
        self.assertEqual(self.store.get(run)['status'], 'succeeded')
